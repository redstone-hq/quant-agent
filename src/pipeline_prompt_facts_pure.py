"""Free-standing prompt-fact helpers: every function here reads its inputs
from its arguments and touches no pipeline state. Lifted VERBATIM out of
`src/pipeline_prompt_facts.py` (the two former `@staticmethod`s are
dedented and shed the decorator; `PromptFactsMixin` re-binds them as
staticmethods so every caller is unchanged).
"""

from src.quantities import avg_dollar_volume, dollar_volumes


def _valuation_signal_from(forward_pe: float | None) -> str:
    """Coarse valuation bucket from forward PE. Conservative thresholds:
    anything < 12 is cheap even for growth names; >= 25 is stretched for
    anything that isn't hyper-growth / secular-leader; 12-25 is fair.
    None → no_data (ETFs, newly-listed, yfinance gap). LLM reads this
    AND the raw PE/PS numbers so it can sector-adjust; the enum is the
    fast first cut that prevents obvious hype-chasing on stretched names.
    """
    if forward_pe is None:
        return "no_data"
    try:
        pe = float(forward_pe)
    except (TypeError, ValueError):
        return "no_data"
    if pe <= 0:
        # Negative / zero forward PE → loss-making; can't judge from PE
        # alone. Treat as no_data so the LLM reasons from other signals.
        return "no_data"
    if pe < 12:
        return "cheap"
    if pe >= 25:
        return "stretched"
    return "fair"


def _missed_ops_quality_metrics(bars: list, lookback_days: int) -> tuple[float | None, float | None, float | None]:
    """Compute (avg_dollar_volume_20d_m, volume_confirmation_ratio,
    single_day_concentration_pct) from a list[OHLCV]-like. All three are
    independent — a symbol with only a few bars may return None for
    dollar-volume while still having a valid single-day concentration.

    Designed for the missed_opportunities digest: thin-liquidity top-
    mover symbols (dollar_vol < $5M) and single-day-gap rallies
    (concentration > 70%) shouldn't dominate the evening LLM's attention.

    Returns (None, None, None) when bars is empty or malformed.
    """
    if not bars or len(bars) < 2:
        return None, None, None

    # 20-day dollar volume via the single shared definition
    # (`src.quantities.avg_dollar_volume`) — this digest and the external-
    # symbol admission gate used to compute the same measure two different
    # ways (a halted session was dropped here and counted there, 5.26%
    # apart on a 20-bar window). Only the THRESHOLDS differ now: $5M here,
    # $10M at admission. `min_bars=5` keeps this caller's deliberate
    # tolerance for short history; the gate demands a full window.
    avg_dvol_m: float | None = None
    vol_conf_ratio: float | None = None
    try:
        dollar_vols = dollar_volumes(bars)
        avg_dvol = avg_dollar_volume(bars)
        if avg_dvol is not None:
            avg_dvol_m = round(avg_dvol / 1_000_000, 2)
            # Today's dollar volume vs the average. >1.5 = buyers showed up.
            if dollar_vols and avg_dvol > 0:
                today_dvol = dollar_vols[-1]
                vol_conf_ratio = round(today_dvol / avg_dvol, 2)
    except (TypeError, ValueError, AttributeError):
        avg_dvol_m = None
        vol_conf_ratio = None

    # Single-day concentration — what fraction of the window's total return
    # came from the biggest single day? > 70% = gap-up day (event/squeeze);
    # < 50% = distributed (trend). Needs ≥ 3 bars in the window to be
    # meaningful (2 bars = one daily return = always 100%).
    window = bars[-(lookback_days + 1) :] if len(bars) > lookback_days else bars
    single_day_conc: float | None = None
    try:
        if len(window) >= 3:
            daily_returns: list[float] = []
            for prev, cur in zip(window[:-1], window[1:]):
                pc_attr = getattr(prev, "close", None)
                cc_attr = getattr(cur, "close", None)
                if not (isinstance(pc_attr, (int, float)) and isinstance(cc_attr, (int, float))):
                    continue
                pc = float(pc_attr)
                cc = float(cc_attr)
                if pc > 0:
                    daily_returns.append((cc - pc) / pc * 100.0)
            if daily_returns:
                total = sum(daily_returns)
                max_abs = max((abs(r) for r in daily_returns), default=0.0)
                # Use absolute totals to avoid sign flips when the window
                # has both up and down days.
                if abs(total) > 0.01:
                    # Percentage of the biggest-day move against total
                    # directional move. Cap at 200 — biggest-day move can
                    # exceed total when subsequent days partially reverse.
                    conc = min(max_abs / abs(total) * 100.0, 200.0)
                    single_day_conc = round(conc, 1)
    except (TypeError, ValueError, AttributeError):
        single_day_conc = None

    return avg_dvol_m, vol_conf_ratio, single_day_conc


def _actualize_trade_row(row: dict) -> dict:
    """Prefer broker-confirmed execution details when present."""
    out = dict(row)
    if out.get("fill_qty"):
        out["qty"] = float(out["fill_qty"])
    if out.get("fill_price"):
        out["price"] = float(out["fill_price"])
    return out


def _build_macro_tech_alignment(
    macro_analysis: dict | None,
    analyses: list,
) -> str:
    """Advisory: does Macro's equity outlook match TA's rating distribution?

    Macro says 'bullish' but TA's ratings are majority bearish → market
    action is diverging from the macro call. That's a signal for PM to
    weight today's TA signals more carefully (market is often right
    about regime flips before FRED data catches up).

    Returns empty string when no divergence, or there's not enough data.
    """
    if not macro_analysis or not analyses:
        return ""
    # macro_analysis is MacroAnalysis (Pydantic) post-Phase-4-#7; dict path
    # still supported for defensive compatibility with legacy callers.
    if hasattr(macro_analysis, "equity_outlook"):
        outlook = (macro_analysis.equity_outlook or "").lower()
    else:
        outlook = (macro_analysis.get("equity_outlook") or "").lower()
    if outlook not in ("bullish", "bearish"):
        return ""
    bullish = sum(1 for a in analyses if a.rating in ("buy", "strong_buy"))
    bearish = sum(1 for a in analyses if a.rating in ("sell", "strong_sell"))
    total = len(analyses)
    if total < 5:
        return ""  # too small a sample to read a tape
    if outlook == "bullish" and bearish > bullish:
        return (
            f"DIVERGENCE: Macro `equity_outlook=bullish` but TA has more bearish "
            f"ratings ({bearish}) than bullish ({bullish}) across {total} symbols. "
            f"Market action may be leading the data — tread carefully on new BUYs "
            f"and respect TA's cautious signals."
        )
    if outlook == "bearish" and bullish > bearish:
        return (
            f"DIVERGENCE: Macro `equity_outlook=bearish` but TA has more bullish "
            f"ratings ({bullish}) than bearish ({bearish}) across {total} symbols. "
            f"Market may be pricing a turnaround before Macro data confirms — "
            f"don't ignore high-R/R long setups just because Macro is cautious."
        )
    return ""
