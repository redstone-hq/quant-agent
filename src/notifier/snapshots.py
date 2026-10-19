"""Position snapshot, earnings / intra-check / meta bodies and the daily CSV.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

from datetime import datetime

from src.notifier.base import (
    _DB_PATH,
    _SWEEP_SYMBOLS,
    logger,
)
from src.notifier.markup import (
    _order_summary,
)
from src.notifier.gaps import (
    _append_coverage_gap_banner,
)


def _append_position_snapshot(lines: list[str], total_value: float | None) -> None:
    """Render top-3 winners + top-3 losers by unrealized P&L from the
    live positions table. Read-only DB hit; degrades gracefully on any
    error (the rest of the message still goes out)."""
    try:
        import sqlite3

        # Default path — same as Database default. If the pipeline
        # config changed it, this snippet won't reflect that; we
        # accept that limitation rather than threading config in.
        if not _DB_PATH.exists():
            return
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            rows = conn.execute(
                "SELECT symbol, qty, avg_entry, current_price, "
                "market_value, unrealized_pnl FROM positions "
                # qty != 0: a short's qty is negative and it is still an open
                # position the operator must see in the evening snapshot.
                "WHERE qty != 0 ORDER BY unrealized_pnl DESC"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("evening position snapshot failed: %s", exc)
        return
    if not rows:
        return
    # The cash-sweep vehicle is parked CASH, not deployed capital (that's its
    # whole contract: hidden from every LLM view, credited as cash by the risk
    # engine, first to liquidate in force_delever). Counting it here reported a
    # ~99%-deployed book on a night the money was entirely in T-bills —
    # inverting the operator's one nightly glance at exposure, and listing SGOV
    # among the P&L movers (2026-07-16 audit).
    parked = sum(r[4] for r in rows if r[0] in _SWEEP_SYMBOLS and r[4] is not None)
    rows = [r for r in rows if r[0] not in _SWEEP_SYMBOLS]
    invested = sum(r[4] for r in rows if r[4] is not None)
    cash_pct = None
    if total_value and total_value > 0:
        cash_pct = max(0.0, (total_value - invested) / total_value * 100)
    summary = f"   Positions: {len(rows)}  invested ${invested:,.0f}"
    if cash_pct is not None:
        summary += f"  ({100 - cash_pct:.0f}% deployed / {cash_pct:.0f}% cash)"
    if parked > 0:
        summary += f"  [+${parked:,.0f} parked in T-bills]"
    lines.append(summary)
    if not rows:
        return

    def _row_line(r: tuple) -> str:
        sym, qty, avg, curr, mv, pnl = r
        sign = "+" if pnl >= 0 else "−"
        # `avg` (avg_entry) or `curr` (current_price) can be NULL (broker
        # race / stale snapshot, same class of gap as the unrealized_pnl
        # NULL handled below). `avg` falsy used to render a fabricated
        # "(+0.0%)" instead of saying the return isn't known, and a NULL
        # `curr` with a real `avg` raised TypeError on `curr / avg`
        # uncaught here — dropping the whole winners/losers block for
        # every row, not just the one with the gap.
        if avg is None or curr is None:
            pct_text = "not available"
        else:
            pct = (curr / avg - 1) * 100
            pct_text = f"{pct:+.1f}%"
        return f"   {sym:<6} {sign}${abs(pnl):>8,.0f}  ({pct_text})"

    # r[5] is positions.unrealized_pnl. SQLite allows NULL on that
    # column (broker race / stale snapshot can leave it unset for a
    # new row), and `None > 0` raises TypeError — which the outer
    # try/except in format_session_result does NOT catch at the
    # right granularity, leaving the operator without the evening
    # snapshot at all. Filter None explicitly. Audit 2026-05-27.
    winners = [r for r in rows if r[5] is not None and r[5] > 0][:3]
    if winners:
        lines.append("📈 Top winners:")
        for r in winners:
            lines.append(_row_line(r))
    losers = [r for r in rows if r[5] is not None and r[5] < 0][-3:][::-1]
    if losers:
        lines.append("📉 Underwater:")
        for r in losers:
            lines.append(_row_line(r))


def _short_date(iso: object) -> str:
    """'2026-09-08' -> 'Sep 8'; anything unparseable is shown as recorded."""
    try:
        d = datetime.strptime(str(iso), "%Y-%m-%d")
    except ValueError:
        return str(iso or "date not recorded")
    return f"{d.strftime('%b')} {d.day}"


def _append_earnings_body(lines: list[str], result: dict) -> None:
    """Fallback body only — the owner-facing pre-earnings message is
    `src.trader_feed._format_earnings`. This names each filing when the
    run recorded them (`result["filings"]`, 2026-09-18) and falls back to
    the bare counts for a result that predates that field."""
    # 2026-09-24: a suspended run (`status: paid_analysis_suspended`) has no
    # `filings` -- the LLM reader never ran -- but it may still carry the
    # `filings_waiting` backlog computed before the circuit tripped. Without
    # this branch the code below falls through to "analyzed:0 confirmed:0
    # failed:0", which reads as "nothing happened" even when N filings are
    # queued and waiting on the circuit to close.
    if result.get("paid_analysis_suspended"):
        waiting = [f for f in (result.get("filings_waiting") or []) if isinstance(f, dict)]
        if waiting:
            lines.append(f"suspended: paid analysis is off, {len(waiting)} filing(s) waiting")
            for row in waiting:
                lines.append(
                    f"  {row.get('symbol', '?')} {row.get('form_type', '')} filed "
                    f"{row.get('filing_date', 'date not recorded')}: waiting"
                )
        else:
            lines.append("suspended: paid analysis is off, no filings waiting")
        return
    filings = [f for f in (result.get("filings") or []) if isinstance(f, dict)]
    if filings:
        for row in filings:
            outcome = "read" if row.get("outcome") == "analyzed" else "could not be read"
            tail = ""
            if row.get("catch_up_read"):
                tail = f" — catch-up read (filed {_short_date(row.get('filing_date'))})"
            lines.append(
                f"{row.get('symbol', '?')} {row.get('form_type', '')} filed "
                f"{row.get('filing_date', 'date not recorded')}: {outcome}{tail}"
            )
        return
    analyzed = result.get("analyzed", 0)
    confirmed = result.get("confirmed", 0)
    failed = result.get("failed", 0)
    lines.append(f"analyzed: {analyzed}  confirmed: {confirmed}  failed: {failed}")


def _append_intra_check_body(lines: list[str], result: dict) -> None:
    # Reaches here when a deterministic breach fired, OR (spec §11.1 guard 3)
    # when an otherwise-OK 30-minute tick found a stop-coverage gap — the
    # sweep's finding is the whole reason that tick broke silence, so it is
    # the first thing on the message.
    _append_coverage_gap_banner(lines, result)
    # Operator wants the details of whatever triggered.
    emergency = result.get("orders") or result.get("emergency_orders") or []
    if emergency:
        lines.append(f"⚠️ EMERGENCY orders: {len(emergency)}")
        for o in emergency[:5]:
            lines.append(f"  {_order_summary(o)}")
    reason = result.get("reason")
    if reason:
        lines.append(f"reason: {reason}")


def _append_meta_body(lines: list[str], result: dict) -> None:
    period = result.get("period")
    if period:
        lines.append(f"period: {period}")
    # audit round 2 (#15/#19): run_quarterly_meta_reflection has no flat
    # "applied"/"rejected" keys — derive the counts from the nested
    # editor_report lists (ApplicationReport.to_dict), same as the evening
    # auto-meta consumer. The old flat reads rendered nothing, ever.
    report = result.get("editor_report") or {}
    applied = len(report.get("applied") or [])
    rej_list = report.get("rejected") or []
    rejected = len(rej_list)
    staged = sum(1 for r in rej_list if isinstance(r, dict) and "dry_run" in str(r.get("reason", "")))
    if applied or rejected:
        lines.append(f"learnings: applied={applied} rejected={rejected}")
        if staged:
            lines.append(f"🧪 {staged} proposal(s) staged for review — data/evolution/{period}/proposed_edits.json")
    elif result.get("proposed_learnings_count"):
        lines.append(
            f"⚠️ {result['proposed_learnings_count']} proposal(s) generated "
            f"but prompt-editor report missing — check logs"
        )
    reason = result.get("reason")
    if reason:
        lines.append(f"reason: {reason}")


def build_daily_csv(closes: list[tuple[str, float]]) -> bytes:
    """Build a P&L history CSV from portfolio_history closes.

    Columns: Date, NAV, Daily P&L, Daily Return %, Drawdown %, SPY Close,
    SPY Return %

    SPY data is fetched via yfinance for the same date range. On any
    yfinance failure the SPY columns are left blank.
    """
    import io, csv, math
    from datetime import datetime, timedelta

    if not closes:
        return b""

    # Fetch SPY closes for the same date range.
    spy_closes: dict[str, float] = {}
    try:
        import yfinance as yf
        import pandas as pd

        earliest = closes[0][0]
        start = (datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
        end_dt = datetime.strptime(closes[-1][0], "%Y-%m-%d") + timedelta(days=2)
        end = end_dt.strftime("%Y-%m-%d")
        df = yf.download("SPY", start=start, end=end, progress=False, auto_adjust=True)
        if not df.empty:
            if hasattr(df.columns, "get_level_values"):
                df.columns = df.columns.get_level_values(0)
            # dropna()+isfinite: a NaN close (data gap / halt) is truthy as a
            # float, so it would slip past the `spy_close and prev_spy` guard,
            # render as "+nan" in the CSV, AND poison prev_spy for every later
            # row. Keep only valid finite closes out of the dict entirely.
            for dt_idx, row in df["Close"].dropna().items():
                val = float(row)
                if math.isfinite(val):
                    spy_closes[str(dt_idx.date())] = val
    except Exception as exc:
        logger.warning("build_daily_csv: SPY fetch failed: %s", exc)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "NAV", "Daily P&L", "Daily Return %", "Drawdown %", "SPY Close", "SPY Return %"])

    prev_nav: float | None = None
    prev_spy: float | None = None
    peak_nav: float | None = None
    for date, nav in closes:
        daily_pnl = nav - prev_nav if prev_nav is not None else 0.0
        daily_ret = (daily_pnl / prev_nav * 100) if prev_nav else 0.0
        peak_nav = max(peak_nav, nav) if peak_nav is not None else nav
        drawdown = (nav - peak_nav) / peak_nav * 100 if peak_nav else 0.0
        spy_close = spy_closes.get(date)
        if spy_close is not None and math.isfinite(spy_close) and prev_spy:
            spy_ret = (spy_close - prev_spy) / prev_spy * 100
        else:
            spy_ret = ""
        writer.writerow(
            [
                date,
                f"{nav:.2f}",
                f"{daily_pnl:+.2f}",
                f"{daily_ret:+.4f}",
                f"{drawdown:+.4f}",
                f"{spy_close:.2f}" if spy_close else "",
                f"{spy_ret:+.4f}" if spy_ret != "" else "",
            ]
        )
        prev_nav = nav
        prev_spy = spy_close if spy_close else prev_spy

    return buf.getvalue().encode("utf-8")
