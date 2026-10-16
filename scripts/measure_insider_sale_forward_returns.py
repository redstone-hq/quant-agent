#!/usr/bin/env python3
"""Board item 63 — what followed the insider SALES the desk has recorded.

Read-only. No LLM call, no broker call, no network, no write to any desk
state. It joins the sale rows the desk already holds (the smart-money
observations cache, the same population `insider_sale_census` samples, or an
exported census JSON) to daily bars the desk already fetched, and prints the
realized forward return per holdings-fraction band.

This MEASURES the instrument. It fits nothing: the horizons come from the
caller, no threshold is searched for, and a row whose return cannot be
resolved from the bars is EXCLUDED and counted, never filled in.

Usage:
    python -m scripts.measure_insider_sale_forward_returns \
        --observations data/smart_money/observations.json \
        --bars scratchpad/zone/bars400.pkl --horizon 1 --horizon 5
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from types import SimpleNamespace

from src.data.insider_signal import holdings_fraction
from src.insider_sale_measure import join_forward_returns, summarize_joined


def _census_rows_from_observations(path: str) -> list[dict]:
    """Cache rows -> census-shaped rows (symbol, date, price, band).

    The cache stores the two share counts SEC Form 4 always reports, so the
    holdings fraction is recomputed with the desk's own
    `src.data.insider_signal.holdings_fraction` rather than re-derived here.
    """
    with open(path) as fh:
        raw = json.load(fh)
    rows = []
    for item in raw:
        if str(item.get("direction") or "") != "sell":
            continue
        probe = SimpleNamespace(
            stream="insider",
            direction="sell",
            shares=item.get("shares"),
            post_transaction_shares=item.get("post_transaction_shares"),
        )
        fraction, _no_prior = holdings_fraction(probe)
        rows.append(
            {
                "symbol": item.get("symbol"),
                "transaction_date": item.get("transaction_date"),
                "reference_price": item.get("price_per_share"),
                "holdings_fraction": fraction,
                "holdings_fraction_band": "sell",
            }
        )
    return rows


def _buy_rows_from_observations(path: str) -> list[dict]:
    """The BUY rows, as the base rate the sales are read against."""
    with open(path) as fh:
        raw = json.load(fh)
    return [
        {
            "symbol": i.get("symbol"),
            "transaction_date": i.get("transaction_date"),
            "reference_price": i.get("price_per_share"),
            "holdings_fraction_band": "buy",
        }
        for i in raw
        if str(i.get("direction") or "") == "buy"
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--observations", default="data/smart_money/observations.json")
    ap.add_argument("--census-json", default=None, help="exported insider_sale_census evidence JSON (uses its `rows`)")
    ap.add_argument("--bars", required=True, help="pickle of SYMBOL -> [daily bars]")
    ap.add_argument(
        "--horizon",
        type=int,
        action="append",
        default=None,
        help="trading sessions after the transaction; repeatable, omit for the filing-to-latest-close window",
    )
    args = ap.parse_args(argv)

    with open(args.bars, "rb") as fh:
        bars_by_symbol = pickle.load(fh)

    if args.census_json:
        with open(args.census_json) as fh:
            sale_rows = json.load(fh).get("rows", [])
    else:
        sale_rows = _census_rows_from_observations(args.observations)
    buy_rows = _buy_rows_from_observations(args.observations) if not args.census_json else []

    horizons = args.horizon if args.horizon else [None]
    out = {"n_sale_rows": len(sale_rows), "n_buy_rows": len(buy_rows), "horizons": {}}
    for h in horizons:
        sales = summarize_joined(join_forward_returns(sale_rows, bars_by_symbol, h))
        buys = summarize_joined(join_forward_returns(buy_rows, bars_by_symbol, h))
        out["horizons"][str(h)] = {"sales": sales, "buys_base_rate": buys}
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
