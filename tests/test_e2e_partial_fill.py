"""Partial fills, end to end: the shares held and the stop resting must agree.

Drives the SAME hermetic morning session as tests/test_e2e_morning_session.py
(real pipeline, risk engine, execution stage, production broker object) but
the broker stand-in fills an order only in part, as a real tape does.

The assertion that matters, read off the broker's own order book: exactly ONE
protective stop rests and its quantity equals the shares actually held.

  buy fills 40%, the rest later   the stop covers the WHOLE fill
  buy fills 40%, then cancelled   the stop covers the 40% only
  sell fills part of a reduction  the smaller position keeps one stop sized to
                                  what is left (the existing book is seeded)

Synthetic symbol and prices only; no network, no broker.
NOT COVERED: stream-driven fills, a re-peg racing a partial, shorts.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from ops.rehearsal import broker as rb
from tests import test_e2e_morning_session as morning
from tests.test_e2e_morning_protection import _seed_company_profile_cache

SYMBOL = morning.SYMBOL


class _PartialClient(rb.RehearsalTradingClient):
    """The rehearsal client, but non-stop orders fill in part.

    `plan`: fraction of the first non-stop order that fills at once;
    `finish_after`: reads after which the remainder fills (None = never,
    the order then stays partially_filled until cancelled).
    """

    plan = 0.4
    finish_after: int | None = None
    sell_plan = 0.5

    def _fills(self):
        self.__dict__.setdefault("_filled", {})
        self.__dict__.setdefault("_reads", {})
        return self._filled, self._reads

    def submit_order(self, request):
        ack = super().submit_order(request)
        order = self.submitted[-1]
        if "stop" in order.order_type:
            return ack
        filled, _ = self._fills()
        frac = self.plan if order.side == "buy" else self.sell_plan
        filled[order.order_id] = float(math.floor(order.qty * frac))
        order.status = "partially_filled"
        ack.status = rb._Status("partially_filled")
        ack.filled_qty = filled[order.order_id]
        return ack

    def _view(self, order):
        filled, reads = self._fills()
        if order.order_id not in filled:
            return None
        reads[order.order_id] = reads.get(order.order_id, 0) + 1
        if (
            self.finish_after is not None
            and order.status == "partially_filled"
            and reads[order.order_id] > self.finish_after
        ):
            order.status = "filled"
            filled[order.order_id] = order.qty
        return filled[order.order_id]

    def get_order_by_id(self, order_id):
        got = super().get_order_by_id(order_id)
        order = self._orders[str(order_id)]
        qty = self._view(order)
        if qty is not None:
            got.status, got.filled_qty = rb._Status(order.status), qty
            got.filled_avg_price = order.limit_price or self._price(order.symbol)
        return got

    def get_orders(self, filter=None):  # noqa: A002
        out = super().get_orders(filter)
        filled, _ = self._fills()
        for o in out:
            if str(o.id) in filled:
                o.filled_qty = filled[str(o.id)]
        return out

    def get_all_positions(self):
        held = {}
        for p in super().get_all_positions():
            held[p.symbol] = p
        filled, _ = self._fills()
        for o in self.submitted:
            q = filled.get(o.order_id, 0.0)
            if not q:
                continue
            p = held.get(o.symbol) or SimpleNamespace(
                symbol=o.symbol,
                qty=0.0,
                avg_entry_price=o.limit_price or 0.0,
                current_price=self._price(o.symbol),
                market_value=0.0,
                unrealized_pl=0.0,
                unrealized_intraday_pl=0.0,
            )
            p.qty = p.qty + q if o.side == "buy" else p.qty - q
            p.market_value = p.qty * p.current_price
            held[o.symbol] = p
        return [p for p in held.values() if p.qty > 0]


def _held(trading) -> float:
    return sum(float(p.qty) for p in trading.get_all_positions() if p.symbol == SYMBOL)


def _resting_stops(trading) -> list:
    return [
        o
        for o in trading._orders.values()
        if o.symbol == SYMBOL
        and o.side == "sell"
        and "stop" in o.order_type
        and o.status in ("new", "pre_existing", "accepted", "partially_filled")
    ]


def _assert_protected_exactly(trading, expect_held: float) -> None:
    held = _held(trading)
    stops = _resting_stops(trading)
    assert held == expect_held, f"expected {expect_held} shares held, broker shows {held}"
    assert len(stops) == 1, f"need exactly ONE resting stop, got {[o.as_plain() for o in stops]}"
    assert float(stops[0].qty) == held, (
        f"resting stop covers {stops[0].qty} but {held} shares are held: "
        f"{[o.as_plain() for o in trading._orders.values()]}"
    )


def _session(tmp_path, monkeypatch, *, plan=0.4, finish_after=None, book_qty=0.0, target_pct=None, sell_plan=0.5):
    """Run the morning session over the partial-fill broker."""
    _PartialClient.plan, _PartialClient.finish_after = plan, finish_after
    _PartialClient.sell_plan = sell_plan
    real_install = rb.install_rehearsal_broker

    def install(broker, snapshot, *, now, **kw):
        if book_qty:
            snapshot.positions.append(
                {
                    "symbol": SYMBOL,
                    "qty": book_qty,
                    "avg_entry": morning.LAST_CLOSE,
                    "current_price": morning.LAST_CLOSE,
                    "market_value": book_qty * morning.LAST_CLOSE,
                    "unrealized_pnl": 0.0,
                }
            )
            snapshot.standing_stops[SYMBOL] = round(morning.RANGE_LOW - 1.0, 2)
            snapshot.cash = snapshot.portfolio_value - book_qty * morning.LAST_CLOSE
        trading = real_install(broker, snapshot, now=now, **kw)
        trading.__class__ = _PartialClient
        return trading

    monkeypatch.setattr(rb, "install_rehearsal_broker", install)
    if target_pct is not None:
        real_answers = morning._scripted_answers

        def answers():
            a = real_answers()
            a["portfolio"]["targets"][0]["target_weight_pct"] = target_pct
            if target_pct == 0.0 and "risk_allocation_pct" in a["portfolio"]["targets"][0]:
                # A whole-position close (owner ruling 2026-10-09: a held
                # position is kept whole or sold whole).
                a["portfolio"]["targets"][0]["risk_allocation_pct"] = 0.0
            if target_pct == 0.0:
                # A close against a 'buy' technical read is recorded as a
                # conflict, as the PM's grounding check requires.
                for prov in a["portfolio"]["targets"][0].get("provenance") or []:
                    if prov.get("source") == "technical":
                        prov["relationship"] = "conflicts"
            return a

        monkeypatch.setattr(morning, "_scripted_answers", answers)
    import src.execution.broker as sb

    monkeypatch.setattr(sb, "_ENTRY_FILL_TIMEOUT_S", 0.05)
    import time as _t

    real_sleep = _t.sleep
    monkeypatch.setattr(_t, "sleep", lambda s: real_sleep(min(s, 0.002)))
    _seed_company_profile_cache(tmp_path)
    return morning._run_session(tmp_path, monkeypatch)


def test_buy_fills_partly_then_the_rest_the_stop_covers_the_whole_fill(tmp_path, monkeypatch):
    result, _, trading = _session(tmp_path, monkeypatch, finish_after=1)
    buy = next(o for o in trading.submitted if o.side == "buy")
    assert buy.status == "filled" and buy.qty > 2
    _assert_protected_exactly(trading, buy.qty)


def test_buy_fills_partly_then_cancelled_the_stop_covers_only_the_part(tmp_path, monkeypatch):
    result, _, trading = _session(tmp_path, monkeypatch, finish_after=None)
    buy = next(o for o in trading.submitted if o.side == "buy")
    part = float(math.floor(buy.qty * 0.4))
    assert 0 < part < buy.qty and buy.status == "canceled", buy.as_plain()
    _assert_protected_exactly(trading, part)


def test_sell_fills_partly_the_smaller_position_keeps_one_correctly_sized_stop(tmp_path, monkeypatch):
    # A WHOLE-position sell that the broker only partly fills. The desk no
    # longer decides partial sells (owner ruling 2026-10-09), but a partial
    # FILL of a full sell still leaves shares held, and the stop must be
    # resized to exactly what is left.
    # Owner 14 Oct 2026: a judgement sale of a holding read as "no chart level"
    # now sells only when it fails a buy rule, and this fixture carries no
    # technical read to judge that. This test is about the stop left after a partly filled sale, so the sale is
    # the chart-CONFIRMED break, which still sells ungated.
    from dataclasses import replace

    from src.exits.structural_protection import StructuralProtection

    _read = StructuralProtection._structural_protection_for_holding

    def _confirmed_break(self, *args, **kwargs):
        check = _read(self, *args, **kwargs)
        return replace(check, basis="structural_level_broken") if check.basis == "no_chart_level" else check

    monkeypatch.setattr(StructuralProtection, "_structural_protection_for_holding", _confirmed_break)
    result, _, trading = _session(tmp_path, monkeypatch, book_qty=60.0, target_pct=0.0)
    sells = [o for o in trading.submitted if o.side == "sell" and "stop" not in o.order_type]
    assert len(sells) == 1, [o.as_plain() for o in trading.submitted]
    assert sells and 0 < trading._filled[sells[0].order_id] < sells[0].qty
    _assert_protected_exactly(trading, 60.0 - trading._filled[sells[0].order_id])
