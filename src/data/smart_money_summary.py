"""One summary line per shortlisted name for the smart-money seat (2026-10-19).

The provider's `fetch` returns EVERY insider and congress row inside its own
lookback window; admission and the cluster census read that full list. The
seat reads only this: one line per name on the run's shortlist, in the
shortlist's order, built with a pandas groupby. Raw rows are never sent — on
19 Oct 21,467 insider rows would have been about 785k tokens.

A shortlisted name with no row carries a recorded reason instead of a line,
the same no-data-reason pattern the news seat uses (`NewsCoverage`).
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from src.models import SmartMoneyObservation

#: Reason recorded for a shortlisted name with no row in either stream.
NO_FILINGS_REASON = "no insider or congress filings in window"


def shortlist_rows(
    observations: Iterable[SmartMoneyObservation], shortlist: Iterable[str]
) -> list[SmartMoneyObservation]:
    """The rows whose symbol is on the shortlist, in fetch order."""
    names = {str(s).strip().upper() for s in shortlist}
    return [row for row in observations if row.symbol.upper() in names]


def _frame(observations: Iterable[SmartMoneyObservation]) -> pd.DataFrame:
    records = []
    for row in observations:
        value = row.transaction_value_usd or 0.0
        insider = row.stream == "insider"
        buy = row.direction == "buy"
        sell = row.direction == "sell"
        records.append(
            {
                "symbol": row.symbol.upper(),
                "insider_buys": int(insider and buy),
                "insider_sells": int(insider and sell),
                "insider_net_usd": (value if buy else -value if sell else 0.0) if insider else 0.0,
                "opportunistic_buys": int(insider and buy and row.signal_class == "opportunistic"),
                "routine_rows": int(row.signal_class == "routine"),
                "congress_buys": int(not insider and buy),
                "congress_sells": int(not insider and sell),
                "amendments": int(row.amendment),
                "ten_b_five_one": int(row.is_10b5_1 is True),
                "transient_admission_eligible": bool(row.transient_admission_eligible),
                "min_lag_days": row.lag_days,
                "buy_date": row.transaction_date if insider and buy else None,
                "buy_usd": value if insider and buy else None,
            }
        )
    return pd.DataFrame.from_records(records)


def _cluster(rows: list[SmartMoneyObservation]) -> dict | None:
    stamped = [r.purchase_cluster for r in rows if r.stream == "insider" and r.purchase_cluster is not None]
    if not stamped:
        return None
    cluster = max(stamped, key=lambda c: c.transaction_date)
    return {
        "transaction_date": cluster.transaction_date.isoformat(),
        "distinct_insiders": cluster.distinct_insiders,
        "combined_value_usd": cluster.combined_value_usd,
    }


def summary_lines(observations: list[SmartMoneyObservation], order: Iterable[str]) -> list[dict]:
    """One line per name with rows, in ``order`` (names absent from it last, by symbol)."""
    if not observations:
        return []
    frame = _frame(observations)
    grouped = frame.groupby("symbol", sort=True)
    totals = grouped.agg(
        insider_net_usd=("insider_net_usd", "sum"),
        insider_buys=("insider_buys", "sum"),
        insider_sells=("insider_sells", "sum"),
        opportunistic_buys=("opportunistic_buys", "sum"),
        routine_rows=("routine_rows", "sum"),
        congress_buys=("congress_buys", "sum"),
        congress_sells=("congress_sells", "sum"),
        amendments=("amendments", "sum"),
        ten_b_five_one=("ten_b_five_one", "sum"),
        transient_admission_eligible=("transient_admission_eligible", "any"),
        min_lag_days=("min_lag_days", "min"),
    )
    latest_buy = (
        frame.dropna(subset=["buy_date"]).sort_values(["buy_date", "buy_usd"]).groupby("symbol").tail(1)
    ).set_index("symbol")
    by_symbol: dict[str, list[SmartMoneyObservation]] = {}
    for row in observations:
        by_symbol.setdefault(row.symbol.upper(), []).append(row)
    position = {str(s).strip().upper(): i for i, s in enumerate(dict.fromkeys(order))}
    names = sorted(totals.index, key=lambda s: (position.get(s, len(position)), s))
    lines = []
    for symbol in names:
        t = totals.loc[symbol]
        buy = latest_buy.loc[symbol] if symbol in latest_buy.index else None
        lines.append(
            {
                "symbol": symbol,
                "insider_net_usd": round(float(t.insider_net_usd), 2),
                "insider_buys": int(t.insider_buys),
                "insider_sells": int(t.insider_sells),
                "opportunistic_buys": int(t.opportunistic_buys),
                "routine_rows": int(t.routine_rows),
                "insider_purchase_cluster": _cluster(by_symbol[symbol]),
                "latest_insider_buy": None
                if buy is None
                else {"date": buy.buy_date.isoformat(), "usd": round(float(buy.buy_usd), 2)},
                "congress_buys": int(t.congress_buys),
                "congress_sells": int(t.congress_sells),
                "amendments": int(t.amendments),
                "ten_b_five_one_rows": int(t.ten_b_five_one),
                "transient_admission_eligible": bool(t.transient_admission_eligible),
                "min_lag_days": int(t.min_lag_days),
            }
        )
    return lines


def no_data_reasons(
    observations: Iterable[SmartMoneyObservation],
    shortlist: Iterable[str],
    *,
    provider_error: str | None = None,
) -> dict[str, str]:
    """Shortlisted name -> why it has no line, in shortlist order."""
    have = {row.symbol.upper() for row in observations}
    reason = f"smart-money fetch failed: {provider_error}" if provider_error and not have else NO_FILINGS_REASON
    return {s: reason for s in dict.fromkeys(str(n).strip().upper() for n in shortlist) if s and s not in have}
