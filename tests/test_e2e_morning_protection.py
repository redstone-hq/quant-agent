"""The hermetic morning session, judged on WHAT it decided and HOW it protected it.

tests/test_e2e_morning_session.py drives the whole session (read it first)
and checks its SHAPE: four stages, in order, every seat before the PM, the
PM before the RM, a BUY reaching the broker stand-in. This file runs the
SAME session once more and checks the part the shape check cannot see:

  size        the buy is the PM's weight of the snapshot's cash, in WHOLE
              shares (fractionability fails closed offline) at the ask
              the desk pays — derived from the inputs, never read back
              from the output and compared with itself;
  protection  exactly one GTC SELL stop rests behind the fill, for its
              whole size, below the entry, at the level the session itself
              recorded, submitted after the fill it protects; the
              broker-truth coverage audit reports no gap;
  the wall    the rehearsal's socket-level network wall (the same one a
              production rehearsal runs under) journalled no attempt to
              leave the box.

Measured 2026-10-02: with protective-stop placement broken to submit
nothing, the session still reported `executed` and the shape test stayed
GREEN; this file is what turns that red.

It lives in its own file because the file-size ratchet refuses growth of
tests/test_e2e_morning_session.py against origin/main, and because the
boundary ratchet counts files that build a TradingPipeline by name — this
one only calls the existing harness.

NOT COVERED: midday / close / evening sessions; an existing book (exits,
rotation, de-levering, stop ratchets, the re-protect path); partial or
unfilled fills; shorts; provider failover under fault; anything a model
seat says — every seat answers from a script.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

from tests.test_e2e_morning_session import LAST_CLOSE, SYMBOL, _assert_full_shape, _run_session

CASH = 10_000.0  # the harness's broker snapshot
TARGET_WEIGHT_PCT = 20.0  # what the scripted PM asks for


def _seed_company_profile_cache(tmp_path: Path) -> None:
    """A warm on-disk identity cache, as production has after any session.

    `CompanyProfileStore` is built with no arguments inside the PM facts
    step and resolves `data/company_profiles.json` against the cwd (the
    harness chdirs to tmp_path); a cold cache sends it to yfinance for the
    company name, which the wall journals as an attempt to leave the box.
    Serving a fresh entry from disk is the production path, not a stub: the
    store never fetches an entry that is fresh.
    """
    from src.data.company import CompanyProfile, CompanyProfileStore

    store = CompanyProfileStore(cache_path=str(tmp_path / "data" / "company_profiles.json"))
    payload = CompanyProfile(symbol=SYMBOL, name="Synthetic Range ETF").as_dict()
    payload["_fetched_at"] = time.time()
    store._cache[SYMBOL] = payload
    store._save()


def _assert_decision_and_protection(result: dict, trading) -> None:
    submitted = list(trading.submitted)
    buys = [o for o in submitted if str(o.side).lower().endswith("buy")]
    stops = [o for o in submitted if str(o.order_type).lower() == "stop"]
    others = [o for o in submitted if o not in buys and o not in stops]
    assert len(buys) == 1, [o.as_plain() for o in submitted]
    assert others == [], f"unexpected orders: {[o.as_plain() for o in others]}"
    buy = buys[0]

    # A plain DAY MARKET order (owner ruling 2026-10-09), sized against the
    # ask it pays; the rehearsal broker quotes the snapshot price on both
    # sides (zero spread), so the ask IS the last close.
    assert str(buy.order_type).lower() == "market", buy.as_plain()
    assert buy.limit_price is None, buy.as_plain()
    buy_price = LAST_CLOSE
    expected_qty = math.floor(CASH * TARGET_WEIGHT_PCT / 100.0 / buy_price)
    assert float(buy.qty) == float(expected_qty), (
        f"PM asked for {TARGET_WEIGHT_PCT}% of ${CASH:,.0f} at "
        f"{buy_price} -> {expected_qty} whole shares; desk sized "
        f"{buy.qty}: {buy.as_plain()}"
    )
    assert buy.status == "filled", buy.as_plain()

    assert len(stops) == 1, (
        f"a filled long needs exactly one protective stop resting; got "
        f"{[o.as_plain() for o in stops]} (all orders: "
        f"{[o.as_plain() for o in submitted]})"
    )
    stop = stops[0]
    assert str(stop.side).lower().endswith("sell"), stop.as_plain()
    assert stop.symbol == buy.symbol, stop.as_plain()
    assert float(stop.qty) == float(buy.qty), f"stop covers {stop.qty} of {buy.qty} held: {stop.as_plain()}"
    assert str(stop.time_in_force).lower() == "gtc", stop.as_plain()
    assert stop.stop_price is not None and 0 < stop.stop_price < buy_price, (
        f"a long's stop must sit below its entry {buy_price}: {stop.as_plain()}"
    )
    assert submitted.index(stop) > submitted.index(buy), "the protective stop must follow the fill it protects"
    recorded = result["orders"][0]
    assert recorded["symbol"] == buy.symbol and recorded["status"] == "filled", recorded
    assert float(recorded["qty"]) == float(buy.qty), recorded
    assert recorded["stop_loss_price"] == stop.stop_price, (
        f"session recorded stop {recorded.get('stop_loss_price')} but the broker was sent {stop.stop_price}"
    )
    assert result["stop_coverage_gaps"] == [], f"broker-truth coverage audit found gaps: {result['stop_coverage_gaps']}"


def test_morning_session_sizes_the_buy_and_protects_it_without_leaving_the_box(
    tmp_path,
    monkeypatch,
):
    from ops.rehearsal.network_wall import no_network

    _seed_company_profile_cache(tmp_path)
    attempts: list[str] = []
    with no_network(attempts):
        result, trace, trading = _run_session(tmp_path, monkeypatch)
    _assert_full_shape(result, trace, trading)
    _assert_decision_and_protection(result, trading)
    assert attempts == [], f"the session tried to leave the box: {attempts}"
