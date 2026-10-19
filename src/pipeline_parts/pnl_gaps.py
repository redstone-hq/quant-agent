import logging
import math

from src.clean_record_baseline import clean_record_baseline
from src.data.news import NewsCoverage
from src.models import (
    NewsIntelligenceReport,
)
from src.pipeline_context import RunContext
from src.sessions.earnings_analyses_session import EarningsAnalysesLoadSession
from src.sessions.news_update_session import NewsUpdateSession

logger = logging.getLogger(__name__)


def _total_pnl_since_reset(
    pipeline,
    total_value: float,
) -> tuple[float | None, float | None, str | None]:
    """`(total_pnl, total_return_pct, since_label)` for the Telegram
    feed's "total P&L" line.

    The baseline starts at the CLEAN RECORD (owner ruling 2026-10-09:
    earlier trading is not evidence; rows stay, only uncounted), not the
    table's oldest row. Baseline, verification against the previous close
    and the "Fri 9 Oct close" label all live in
    `src.clean_record_baseline.clean_record_baseline`, shared with the
    dashboard.

    Returns `(None, None, None)` when the baseline is missing or cannot be
    verified — never a fabricated 0.
    """
    try:
        first, prev = pipeline.db.get_clean_record_baseline_rows()
    except Exception as exc:  # noqa: BLE001
        logger.warning("total P&L baseline lookup failed: %s", exc)
        return None, None, None
    resolved = clean_record_baseline(first, prev)
    if resolved is None:
        return None, None, None
    baseline, since_label = resolved
    try:
        tv = float(total_value)
    except (TypeError, ValueError):
        return None, None, None
    if not math.isfinite(tv):
        return None, None, None
    total_pnl = tv - baseline
    return total_pnl, total_pnl / baseline * 100, since_label


def _forced_close_side_and_qty(position_qty: float) -> tuple[str, float] | None:
    """Direction-aware sizing for a FORCED close — the §11.2
    de-levering ladder's forced trim, or an operator kill. NOT the
    normal decision
    path: SELL decisions and the portfolio constructor keep
    refusing a negative qty exactly as before (see _full_sell_qty
    and the Stage 1 guard in portfolio_constructor.py
    — shorts still cannot be opened or covered through that path).

    Returns ``(side, qty)`` where ``side`` is ``'sell'`` to flatten a
    long or ``'buy'`` to cover a short, and ``qty`` is the ABSOLUTE
    number of shares — always positive, never the signed broker qty.

    Returns ``None`` when direction can't be determined (qty is zero,
    NaN, or otherwise not a finite nonzero number). This is the one
    design rule the reviewer called non-negotiable: a forced close is
    only safe when the side is certain, because guessing wrong on a
    short doesn't fail safe — a SELL aimed at a position that's
    actually already short would ADD to the short (sell more of a
    symbol you don't hold long), doubling the very exposure the
    forced close exists to shed. Refusing and logging loudly beats
    guessing every time; the caller is responsible for the loud log,
    this just refuses to hand back an answer to guess with.
    """
    if not isinstance(position_qty, (int, float)) or not math.isfinite(position_qty):
        return None
    if position_qty == 0:
        return None
    if position_qty > 0:
        return "sell", float(position_qty)
    return "buy", float(-position_qty)


def _trade_executed_or_pending(trade: dict) -> bool:
    """True when a trade either executed or is still an open live attempt.

    Used for idempotence checks on sell-side rows (same-day trim
    discipline): a pending submitted trim should block a duplicate order,
    but a canceled/rejected/expired zero-fill should not.
    """
    status = str(trade.get("fill_status") or "").lower()
    if not status:
        return True
    if status in {"submitted", "filled"}:
        return True
    try:
        return float(trade.get("fill_qty") or 0) > 0
    except (TypeError, ValueError):
        return False


def _record_short_overnight_gaps(pipeline, positions) -> None:
    """Store the adverse overnight gap suffered by each held SHORT.

    SHORT-SIDE GAP EVIDENCE, RECORDING ONLY — item 186. The short-side
    sizing haircut is unsourced and two attempts to read it off the
    instrument have failed; both failed because this desk has never
    kept a record of what a short actually suffers overnight. Bars are
    fetched live and discarded and there is no OHLCV table, so the
    evidence has to be captured beside the trade while the trade is
    open. Nothing reads this back: no threshold, no gate, no sizing
    change. See `TradeStore.record_overnight_gap` for the hard limit on
    its use.

    Shorts only, because only a short's loss above its stop is
    unbounded and only the short-side multiple is the open question.
    Held shorts are a handful at most, so the two-bar fetch per name is
    cheap. Fail-soft per symbol and as a whole: a recording problem
    must never disturb a trading session.
    """
    for p in positions or []:
        try:
            if float(getattr(p, "qty", 0) or 0) >= 0:
                continue
            bars = pipeline.market.get_ohlcv(p.symbol, 7) or []
            if len(bars) < 2:
                continue
            prev_bar, today = bars[-2], bars[-1]
            pipeline.db.record_overnight_gap(
                p.symbol,
                prev_bar.close,
                today.open,
                str(today.date),
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "overnight-gap recording skipped for %s",
                getattr(p, "symbol", "?"),
                exc_info=True,
            )


def _run_news_update(
    pipeline,
    run_id: str,
    session: str = "morning",
    universe: list[str] | None = None,
    held_symbols: list[str] | None = None,
    candidate_symbols: list[str] | None = None,
) -> "tuple[NewsIntelligenceReport | None, NewsCoverage | None]":
    """Thin shim: builds the standalone session and runs it (body moved to src/sessions/news_update_session.py)."""
    return NewsUpdateSession(
        config=pipeline._collab("config"),
        db=pipeline._collab("db"),
        news_analyst=pipeline._collab("news_analyst"),
        news_provider=pipeline._collab("news_provider"),
        news_store=pipeline._collab("news_store"),
    ).run(run_id, session, universe, held_symbols, candidate_symbols)


def _load_earnings_analyses(
    pipeline,
    run_id: str,
    session: str = "morning",
    ctx: RunContext | None = None,
    universe: list[str] | None = None,
) -> tuple[list, list]:
    """Thin shim: builds the standalone session and runs it (body moved to src/sessions/earnings_analyses_session.py)."""
    return EarningsAnalysesLoadSession(
        config=pipeline._collab("config"),
        earnings_analyst=pipeline._collab("earnings_analyst"),
        earnings_provider=pipeline._collab("earnings_provider"),
    ).run(run_id, session, ctx, universe)


def _earnings_preprocess_symbols(pipeline) -> list[str]:
    """Configured universe, Form-4 admission-eligible names, then last night's screened morning names.

    2026-09-16: preprocess returned `nothing_new` against the configured
    universe while FTK/RSG were already Form-4 hot. Morning then saw
    those filings as placeholders. The hot list is whatever the
    already-refreshed provider marks `admission_eligible` — not an
    invented "preprocess N names" cap. Morning's broker-quality gate
    and `max_external_candidates` still decide who actually trades.

    2026-10-19: the screened names the morning admits (see
    `_screened_morning_symbols`) are checked too, so a new filing for one
    is analysed before the open instead of being downloaded and left
    unread (ASAN's 10-Q, 16 Oct). Only NEW filings are analysed, so the
    added cost is bounded by how many of those names filed overnight.
    """
    configured = [
        str(symbol).strip().upper() for symbol in (pipeline.config.trading.universe or []) if str(symbol).strip()
    ]
    hot = _form4_hot_symbols(pipeline, configured)
    screened = _screened_morning_symbols(pipeline, set(configured) | set(hot))
    if screened:
        logger.info(
            "Earnings preprocess: adding %d screened morning symbol(s) to the filing check",
            len(screened),
        )
    return configured + hot + screened


def _form4_hot_symbols(pipeline, configured: list[str]) -> list[str]:
    """Form-4 admission-eligible names outside the configured universe (empty on any failure)."""
    hot: list[str] = []
    try:
        if not getattr(pipeline.config.smart_money, "enabled", False):
            return []
        provider = getattr(pipeline, "smart_money_provider", None)
        if provider is None or not hasattr(provider, "fetch"):
            return []
        observations, _err = provider.fetch(configured)
        seen = set(configured)
        for item in observations or []:
            if not bool(getattr(item, "admission_eligible", False)):
                continue
            symbol = str(getattr(item, "symbol", "") or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            hot.append(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Earnings preprocess: hot-admit symbol union failed (%s) — falling back to the configured universe",
            exc,
        )
        return []
    if hot:
        logger.info(
            "Earnings preprocess: adding %d Form-4 admission-eligible symbol(s) to the filing check: %s",
            len(hot),
            ", ".join(hot),
        )
    return hot


def _screened_morning_symbols(pipeline, already: set[str]) -> list[str]:
    """The screened names this morning will admit (empty on any failure).

    Calls the morning's own rule, `src.stage_morning_research.choose_screened_names`,
    on last night's passers (`_admit_screened_universe_symbols`) and ignores
    its dropped names (the morning records those).
    """
    from src.stage_morning_research import choose_screened_names

    try:
        ranked, details = pipeline.admission._admit_screened_universe_symbols()
        if not details:
            return []
        try:
            held = {str(getattr(p, "symbol", "")).strip().upper() for p in pipeline.broker.get_positions() or []}
        except Exception:  # noqa: BLE001 — held passers are a bonus, ranked names still go
            held = set()
        held_passers, sent, _dropped = choose_screened_names(
            ranked=ranked,
            screened_details=details,
            already=already,
            held=held,
            top_n=int(pipeline.config.universe_screen.morning_top_n),
        )
        return held_passers + sent
    except Exception as exc:  # noqa: BLE001
        logger.warning("Earnings preprocess: screened morning names unavailable (%s) — not added", exc)
        return []
