"""src.prompt_facts.missed_ops_signals -- the per-symbol signals the missed-opportunities digest and the thesis-health review are built from.

Bodies moved verbatim from src/pipeline_prompt_facts.py (originally src/pipeline.py),
item 210 step 10b. Every collaborator is an explicit keyword-only constructor argument;
nothing here imports src.pipeline. Read-only: places no orders, cancels nothing, amends
no stop. Any prompt text these helpers emit is byte-identical to the pre-move text.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
import re
import json as _json
from src.pipeline_prompt_facts_pure import (
    _missed_ops_quality_metrics,
    _valuation_signal_from,
)
from src.risk.metrics import unrealized_pnl_pct
from src.trading_calendar import et_today, session_date_key, to_et
from src.sentinel.counted import record_swallowed

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


def _et_instant(ts) -> datetime | None:
    """A stored timestamp as an aware ET datetime (naive = UTC, as SQLite
    writes it), or None when missing or unparseable."""
    try:
        return to_et(datetime.fromisoformat(str(ts).strip()))
    except (TypeError, ValueError):
        return None


class MissedOpsSignals:
    """Tech/news/earnings/theme/sector signals for the missed-ops digest and thesis-health review."""

    def __init__(
        self,
        *,
        db,
        news_store,
        earnings_provider,
        macro_store,
        parse_logged_agent_response,
        broker,
        market,
        config,
    ) -> None:
        self.db = db
        self.news_store = news_store
        self.earnings_provider = earnings_provider
        self.macro_store = macro_store
        self._parse_logged_agent_response = parse_logged_agent_response
        self.broker = broker
        self.market = market
        self.config = config

    def _missed_ops_held_set(self, lookback_days: int, current_position_symbols: set[str]) -> set[str]:
        """Symbols we owned (or traded) within the window.

        Union of (a) symbols currently open in ctx.positions and (b) symbols
        with any executed trade in the last ~2×`lookback_days` calendar days
        (accounts for weekends / holidays). Over-inclusive on purpose — better
        to NOT flag a legitimate hold as "missed" than invent a miss from a
        stale SELL earlier in the week.
        """
        from datetime import timedelta

        held: set[str] = {s.upper() for s in current_position_symbols if s}
        try:
            rows = self.db.get_trades(limit=500, executed_only=True)
        except Exception as exc:
            logger.warning("missed_ops: get_trades failed: %s", exc)
            return held
        cutoff = et_today() - timedelta(days=lookback_days * 2 + 2)
        cutoff_str = cutoff.isoformat()
        for r in rows:
            ts_date = (r.get("timestamp") or "")[:10]
            if not ts_date or ts_date < cutoff_str:
                continue
            sym = (r.get("symbol") or "").upper()
            if sym:
                held.add(sym)
        return held

    def _missed_ops_tech_signal(self, lookback_days: int) -> dict[str, tuple[str, str]]:
        """Most recent TA rating per symbol in window → {symbol: (rating, date)}.

        Walks recent tech_analyst agent_logs, parses the batch-output JSON,
        takes the newest rating per symbol. `rating in ("buy","strong_buy")`
        is what drives the `had_ta_signal` flag downstream.

        Production tech_analyst emits two different JSON shapes depending on
        which code path wrote the log — either ``{"analyses": [...]}`` or a
        BARE LIST of per-symbol dicts. We delegate shape normalization to
        `quarterly_digest._tech_analyses_from_data` so both paths stay in
        sync — adding a third shape should only require editing that helper.
        """
        from datetime import timedelta
        from src.evolution.quarterly_digest import _tech_analyses_from_data

        try:
            rows = self.db.get_recent_agent_outputs(
                agent_name="tech_analyst",
                limit=lookback_days * 3,
                before_date=None,
            )
        except Exception as exc:
            record_swallowed("missed_ops.tech_signal", exc, log=logger)
            return {}
        cutoff_str = (et_today() - timedelta(days=lookback_days * 2 + 2)).isoformat()
        latest: dict[str, tuple[str, str]] = {}
        for row in rows:
            ts_date = (row.get("timestamp") or "")[:10]
            if not ts_date or ts_date < cutoff_str:
                continue
            data = self._parse_logged_agent_response(row)
            if data is None:
                continue
            for a in _tech_analyses_from_data(data):
                sym = (a.get("symbol") or "").upper()
                rating = a.get("rating")
                if not sym or not rating:
                    continue
                if sym not in latest:  # newer rows first from get_recent_agent_outputs
                    latest[sym] = (str(rating), ts_date)
        return latest

    def _missed_ops_news_signal(self, lookback_days: int) -> dict[str, str]:
        """Most recent news headline touching each symbol in window.

        Walks dated full_report.json files. For state_changes, harvests
        (event-text, affected_symbols) pairs. For stock_news, takes the first
        alert's headline. Newest day wins. Headlines clipped to 140 chars so
        they don't blow the prompt budget.
        """
        import json as _json
        from datetime import timedelta
        from pathlib import Path

        news_dir = getattr(self.news_store, "data_dir", None)
        if news_dir is None:
            return {}
        out: dict[str, str] = {}
        today = et_today()
        # Iterate newest → oldest so first-seen wins (freshest headline per symbol).
        for days_ago in range(lookback_days + 1):
            day = today - timedelta(days=days_ago)
            report_path = Path(news_dir) / str(day) / "full_report.json"
            if not report_path.exists():
                continue
            try:
                report = _json.loads(report_path.read_text())
            except (_json.JSONDecodeError, OSError):
                continue
            for ch in report.get("state_changes", []) or []:
                event = (ch.get("event") or "").strip()
                if not event:
                    continue
                for sym in ch.get("affected_symbols", []) or []:
                    sym_u = str(sym).upper()
                    if sym_u and sym_u not in out:
                        out[sym_u] = event[:140]
            for sym, items in (report.get("stock_news") or {}).items():
                sym_u = str(sym).upper()
                if sym_u in out or not items:
                    continue
                first = items[0] if isinstance(items, list) else None
                if isinstance(first, dict):
                    headline = (first.get("headline") or "").strip()
                    if headline:
                        out[sym_u] = headline[:140]
        return out

    def _missed_ops_theme_tags(self, lookback_days: int) -> dict[str, list[str]]:
        """Rough theme proxies per symbol from recent state_change event text.

        Extracts the first 1-2 meaningful tokens from each event and tags the
        affected symbols with them. Not a semantic classifier — the LLM
        refines to one canonical theme name in `MissedOpportunity.theme_if_any`.
        Purpose here is surface pattern co-occurrence ("AVGO: ai-capex, compute")
        so the LLM can spot the theme instead of treating each headline
        in isolation.
        """
        import json as _json
        import re
        from datetime import timedelta
        from pathlib import Path

        news_dir = getattr(self.news_store, "data_dir", None)
        if news_dir is None:
            return {}
        out: dict[str, list[str]] = {}
        stopwords = {
            "this",
            "that",
            "with",
            "from",
            "into",
            "than",
            "will",
            "would",
            "should",
            "could",
            "about",
            "against",
            "between",
            "report",
        }
        today = et_today()
        for days_ago in range(lookback_days + 1):
            day = today - timedelta(days=days_ago)
            report_path = Path(news_dir) / str(day) / "full_report.json"
            if not report_path.exists():
                continue
            try:
                report = _json.loads(report_path.read_text())
            except (_json.JSONDecodeError, OSError):
                continue
            for ch in report.get("state_changes", []) or []:
                event = (ch.get("event") or "").strip()
                tokens = [t.lower() for t in re.findall(r"[A-Za-z]{4,}", event) if t.lower() not in stopwords]
                if not tokens:
                    continue
                tag = "-".join(tokens[:2])
                for sym in ch.get("affected_symbols", []) or []:
                    sym_u = str(sym).upper()
                    if not sym_u:
                        continue
                    bucket = out.setdefault(sym_u, [])
                    if tag not in bucket and len(bucket) < 4:
                        bucket.append(tag)
        return out

    def _missed_ops_earnings_signal(self, session_earnings: list[dict] | None = None) -> dict[str, str]:
        """Most recent non-bearish earnings take per symbol from on-disk cache.

        Walks earnings_provider.manifest, skips abandoned entries, reads each
        analysis file's head (first 600 chars) and passes any entry whose
        head text contains no "bearish" token. Returns {symbol: snippet} where
        snippet is a clipped first-sentence-ish summary the LLM can cite as
        evidence for `fundamentals_mispricing` classification.
        """
        try:
            manifest = getattr(self.earnings_provider, "manifest", {}) or {}
        except Exception:
            return {}
        from pathlib import Path

        from src.agents.portfolio_manager.evidence_prompting import earnings_stance_is_stale

        # Currency is the one rule `earnings_stance_is_stale`. The session's
        # already-fetched earnings list (`current_until` set from this
        # session's SEC check) decides for the symbols it covers; any other
        # symbol falls back to the stored manifest stamp (no network, no
        # manifest write; an absent stamp is stale). No age cutoff here.
        # Sentiment comes from the STRUCTURED "Sentiment:" line - a naive
        # `"bearish" in head` substring dropped NEUTRAL analyses whose prose
        # merely mentioned the word ("not bearish", "bearish scenarios considered").
        session_until: dict[str, str] = {}
        for item in session_earnings or []:
            if isinstance(item, dict) and item.get("symbol"):
                session_until[str(item["symbol"]).strip().upper()] = str(item.get("current_until") or "")

        best: dict[str, tuple[str, dict]] = {}  # symbol -> (filing_date, entry)
        for key, entry in manifest.items():
            if not isinstance(entry, dict) or entry.get("abandoned"):
                continue
            symbol = str(key).split("_")[0].upper()
            fd = str(entry.get("filing_date") or "")
            if symbol not in best or fd > best[symbol][0]:
                best[symbol] = (fd, entry)

        out: dict[str, str] = {}
        today = et_today()
        for symbol, (_fd, entry) in best.items():
            current_until = session_until[symbol] if symbol in session_until else entry.get("current_until")
            if earnings_stance_is_stale(current_until, today):
                continue
            analysis_path = entry.get("analysis_path")
            if not analysis_path:
                continue
            p = Path(analysis_path)
            if not p.exists():
                continue
            try:
                text = p.read_text()
            except OSError:
                continue
            head = text[:600]
            m = re.search(r"^\s*-?\s*\*{0,2}Sentiment\*{0,2}\s*:\s*(\w+)", head, re.MULTILINE | re.IGNORECASE)
            sentiment = m.group(1).lower() if m else None
            if sentiment == "bearish":
                continue
            if sentiment is None and "bearish" in head.lower():
                continue  # no structured line — keep the conservative fallback
            snippet = head.replace("\n", " ").strip()[:140]
            if snippet:
                out[symbol] = snippet
        return out

    def _missed_ops_macro_sector_map(self) -> dict[str, str]:
        """Latest macro sector stance: {sector: bullish|neutral|bearish}.

        Reads macro_store.load_last_state() — persisted at the end of each
        morning macro run. Missing keys / stances → empty dict, snapshot
        defaults to "unknown" for each symbol, which is itself a signal (if
        macro never covers a whole sector we rally through, that's a
        coverage blindspot the quarterly meta-reflector should notice).
        """
        try:
            state = self.macro_store.load_last_state() or {}
        except Exception as exc:
            record_swallowed("missed_ops.macro_sector_map", exc, log=logger)
            return {}
        guidance = state.get("sector_guidance") or {}
        if not isinstance(guidance, dict):
            return {}
        out: dict[str, str] = {}
        for sector, stance in guidance.items():
            if isinstance(stance, str) and stance in ("bullish", "neutral", "bearish"):
                out[str(sector)] = stance
        return out

    def _thesis_tech_trajectory_map(
        self,
        opens: dict,
    ) -> dict[str, list[str]]:
        """Per held symbol, ONE tech rating per ET session since the position
        opened -- the session's LAST rating -- as "YYYY-MM-DD: rating", newest
        session first. `opens` maps symbol -> the ET instant its position
        opened (see `_thesis_position_open`).

        A tech row counts only when its OWN timestamp is at or after that
        symbol's open. The read is bounded by the earliest open, not by a row
        count: the tech seat writes many rows a session, so a fixed row cap
        returned only today's intraday runs. Uses the same shape-normalizer
        as the missed_ops digest. Empty dict on failure."""
        from src.evolution.quarterly_digest import _tech_analyses_from_data
        from src.storage.analytics.agent_log_reads import count_recent_agent_outputs

        if not opens:
            return {}
        since_key = session_date_key(min(opens.values()))
        try:
            # Rows on or after the earliest open's ET session = all rows minus
            # those before it -- the same ET-midnight predicate both ways.
            total = count_recent_agent_outputs(conn=self.db.conn, lock=self.db._lock, agent_name="tech_analyst")
            older = count_recent_agent_outputs(
                conn=self.db.conn, lock=self.db._lock, agent_name="tech_analyst", before_date=since_key
            )
            rows = self.db.get_recent_agent_outputs(
                agent_name="tech_analyst",
                limit=max(0, total - older),
                before_date=None,
            )
        except Exception as exc:
            record_swallowed("missed_ops.thesis_tech_trajectory", exc, log=logger)
            return {}
        by_sym: dict[str, list[str]] = {}
        seen: set[tuple[str, str]] = set()
        for row in rows:  # newest first, so a session's first hit is its last rating
            at = _et_instant(row.get("timestamp"))
            if at is None:
                continue
            session = session_date_key(at)
            data = self._parse_logged_agent_response(row)
            if data is None:
                continue
            for a in _tech_analyses_from_data(data):
                sym = (a.get("symbol") or "").upper()
                rating = a.get("rating")
                if not sym or not rating or sym not in opens or at < opens[sym]:
                    continue
                if (sym, session) in seen:
                    continue
                seen.add((sym, session))
                by_sym.setdefault(sym, []).append(f"{session}: {rating}")
        return by_sym

    def _thesis_news_events_map(
        self,
        opens: dict,
    ) -> dict[str, list[dict]]:
        """Per held symbol, news events dated on or after the ET session its
        position opened. Returns {sym: [{event, conviction, date}, ...]}
        newest-first. `opens` maps symbol -> the ET instant its position
        opened (see `_thesis_position_open`).

        Walks the dated full_report.json files from today back to the
        earliest open's session; a state_change naming the symbol in
        affected_symbols is kept when the report's date is not before that
        symbol's open session.
        """
        import json as _json
        from datetime import timedelta
        from pathlib import Path

        news_dir = getattr(self.news_store, "data_dir", None)
        if news_dir is None or not opens:
            return {}
        open_day = {sym: at.date() for sym, at in opens.items()}
        out: dict[str, list[dict]] = {}
        today = et_today()
        for days_ago in range((today - min(open_day.values())).days + 1):
            day = today - timedelta(days=days_ago)
            report_path = Path(news_dir) / str(day) / "full_report.json"
            if not report_path.exists():
                continue
            try:
                report = _json.loads(report_path.read_text())
            except (_json.JSONDecodeError, OSError):
                continue
            for ch in report.get("state_changes", []) or []:
                event = (ch.get("event") or "").strip()
                if not event:
                    continue
                affected = ch.get("affected_symbols", []) or []
                conviction = (ch.get("conviction") or "").lower()
                for sym in affected:
                    sym_u = str(sym).upper()
                    if sym_u not in open_day or day < open_day[sym_u]:
                        continue
                    out.setdefault(sym_u, []).append(
                        {
                            "event": event[:140],
                            "conviction": conviction,
                            "date": str(day),
                        }
                    )
        return out

    def _thesis_position_open(self, p) -> dict | None:
        """The row that OPENED held position `p` -- its first filled opening
        row on the current position_id -- so a top-up neither resets the
        holding age nor moves the review window. Same lookup the
        morning/midday facts use (`_position_open_row`); a legacy row with no
        position_id fails closed onto the newest opening row."""
        from src.pipeline_prompt_facts import _position_open_row

        opening = "SHORT" if (p.qty or 0) < 0 else "BUY"
        try:
            newest = self.db.get_symbol_last_buy(p.symbol, action=opening)
        except Exception:
            newest = None
        return _position_open_row(self.db, newest, p.symbol, opening) if newest else None

    def _build_thesis_health_context(
        self,
        positions,
    ) -> dict[str, dict]:
        """Per-position fundamental-evolution snapshot for the evening
        thesis_health_review step.

        Every window starts at the POSITION'S OPEN (its first opening row on
        the current position_id), so a top-up never makes a held trade look
        new. For each held symbol, gather:
          - Entry context (date, price, days_held, sessions_held,
            original thesis text) — `sessions_held` is the weekend/holiday-aware
            trading-session count the reviewer reads for pace (item 165)
          - Tech rating trajectory (one rating per session since the open)
          - News mentions count + 2 latest headlines since the open
          - Most recent earnings sentiment + key_thesis
          - Current macro sector stance
          - Valuation snapshot (trailing PE / forward PE / P/S / signal)

        Shape designed so the evening LLM can answer
        "strengthening / intact / weakening / broken" per holding,
        not just aggregate-level "bullish / bearish". That step is
        what separates a swing-trader feedback bot from a value-
        investor strategic reflection.

        Returns {symbol: dict}. Empty dict when there are no positions.
        Exceptions during data fetch degrade gracefully — a missing
        field is None or [], the helper does not raise. A position whose
        open cannot be found gets no tech/news window (unknown, not guessed).
        """
        if not positions:
            return {}

        open_rows = {p.symbol: self._thesis_position_open(p) for p in positions}
        opens = {}
        for sym, row in open_rows.items():
            at = _et_instant((row or {}).get("timestamp"))
            if at is not None:
                opens[sym] = at
        tech_map_multi = self._thesis_tech_trajectory_map(opens)
        news_events_map = self._thesis_news_events_map(opens)
        earnings_map = self._missed_ops_earnings_signal()
        macro_map = self._missed_ops_macro_sector_map()

        out: dict[str, dict] = {}
        for p in positions:
            sym = p.symbol

            # Entry context
            entry_date: str | None = None
            entry_reasoning = ""
            days_held: int | None = None
            # Item 165: the reviewer judges PACE ("too slow" → exit early) off
            # holding time, so holding time here must be in TRADING SESSIONS,
            # not calendar days. A weekend or market holiday adds calendar days
            # and zero sessions, so a calendar-day count made a good position
            # look slower than it is and could trigger a real-money early exit.
            # `sessions_held` is the weekend/holiday-aware count the reviewer
            # prompt reads for pace, computed with the SAME helper the
            # morning/midday facts path uses (`broker.trading_sessions_held`,
            # backed by Alpaca's real market calendar) — no parallel method.
            # Counted from the position's OPEN, never the newest top-up.
            sessions_held: int | None = None
            open_row = open_rows.get(sym)
            if open_row:
                if sym in opens:
                    entry_d = opens[sym].date()
                    entry_date = entry_d.isoformat()
                    days_held = max(0, (et_today() - entry_d).days)
                    try:
                        sessions_held = self.broker.trading_sessions_held(
                            entry_d,
                            et_today(),
                        )
                    except Exception:
                        # Calendar/broker hiccup: degrade this one field to
                        # None rather than dropping the whole thesis-health
                        # row, matching the graceful-degradation contract above.
                        sessions_held = None
                entry_reasoning = (open_row.get("reasoning") or "")[:300]

            # P&L% — the one definition (`src.risk.metrics.unrealized_pnl_pct`),
            # which returns None when genuinely unknowable. The `cost > 0`
            # guard this replaces silently returned None for EVERY short
            # (a short's `avg_entry * qty` is negative), so evening's
            # thesis-health review saw no P&L on the short book at all.
            _pnl_pct = unrealized_pnl_pct(p)
            pnl_pct = None if _pnl_pct is None else round(_pnl_pct, 2)

            # Tech trajectory — the last 4 sessions' ratings since the open
            tech_trajectory = tech_map_multi.get(sym, [])[:4]

            # News — total count since the open + latest 2 headlines
            news_events = news_events_map.get(sym, [])
            news_count = len(news_events)
            latest_news_headlines = [e["event"] for e in news_events[:2]]

            # Sector stance
            sector = ""
            try:
                from src.sector_reference import _get_sector

                sector = _get_sector(sym) or ""
            except Exception:
                sector = ""
            macro_stance = macro_map.get(sector, "unknown") if sector else "unknown"

            # Valuation — bounded per-symbol yfinance call
            valuation = {
                "trailing_pe": None,
                "forward_pe": None,
                "ps_ratio": None,
            }
            try:
                v = self.market.get_valuation_metrics(sym) or {}
                valuation["trailing_pe"] = v.get("trailing_pe")
                valuation["forward_pe"] = v.get("forward_pe")
                valuation["ps_ratio"] = v.get("ps_ratio")
            except Exception:
                pass
            valuation["signal"] = _valuation_signal_from(valuation["forward_pe"])

            # Earnings deep-dive: full reasoning_chain + headline metrics
            # from the canonical analysis_*.md for this symbol. Only
            # surfaced for HELD positions (token-budget reasons); missed_ops
            # still use the 140-char snippet via earnings_map.
            from src.data.earnings_deep_dive import load_earnings_deep_dive

            deep_dive = None
            try:
                manifest = getattr(self.earnings_provider, "manifest", {}) or {}
                deep_dive = load_earnings_deep_dive(sym, manifest)
            except Exception as exc:
                logger.debug(
                    "thesis_health earnings deep-dive failed for %s: %s",
                    sym,
                    exc,
                )

            out[sym] = {
                "symbol": sym,
                "entry_date": entry_date,
                "entry_reasoning": entry_reasoning,
                "days_held": days_held,
                "sessions_held": sessions_held,
                "entry_price": p.avg_entry,
                "current_price": p.current_price,
                "pnl_pct": pnl_pct,
                "sector": sector,
                "tech_trajectory": tech_trajectory,
                "news_count_since_open": news_count,
                "latest_news_headlines": latest_news_headlines,
                "recent_earnings_signal": earnings_map.get(sym),
                "earnings_deep_dive": deep_dive,
                "macro_sector_stance": macro_stance,
                "valuation": valuation,
            }
        return out

    def _build_missed_opportunities_digest(
        self,
        lookback_days: int = 5,
        move_threshold_pct: float = 8.0,
        top_n: int = 15,
        top_movers_count: int = 15,
        current_position_symbols: set[str] | None = None,
        min_top_mover_dollar_volume_m: float = 5.0,
        session_earnings: list[dict] | None = None,
    ) -> list:
        """Notable movers we did NOT own — input for evening's missed-op review.

        Symbol set = trading universe ∪ Alpaca top gainers. For each, compute
        the `lookback_days` window return; keep those crossing
        `move_threshold_pct` (absolute). Tag each with the signal state that
        was visible at the time (prior TA rating, news headline, earnings
        sentiment, macro sector stance) so the LLM's miss classification has
        to cite observable evidence, not retro-rationalize price.

        Quality filter for TOP-MOVER symbols only (universe symbols always
        pass — they're curated): if 20-day avg dollar volume is below
        `min_top_mover_dollar_volume_m` (default $5M), the symbol is
        dropped before reaching the LLM. Thin-liquidity gappers aren't
        interesting to a medium-long-term investor and flooding the prompt
        with them dilutes the real misses.

        Returns a list[MissedOpportunitySnapshot]. Empty when no symbol
        crosses the threshold. Sort order within the list:
          (a) not-held, has prior signal — real "we saw it, didn't act" misses
          (b) not-held, no prior signal — theme-coverage blindspots
          (c) already held — context for decision-quality review
        Within each group by |move_pct| descending. Top `top_n` only.
        """
        from src.models import MissedOpportunitySnapshot

        universe = list(getattr(self.config.trading, "universe", []) or [])
        universe_set = {s.upper() for s in universe if s}
        try:
            top_movers = self.broker.get_top_movers(n=top_movers_count) or []
        except Exception as exc:
            logger.warning("missed_ops: get_top_movers failed: %s", exc)
            top_movers = []
        top_mover_syms = {str(m["symbol"]).upper() for m in top_movers if isinstance(m, dict) and m.get("symbol")}
        all_syms = universe_set | top_mover_syms
        if not all_syms:
            return []

        # Fetch bars once per symbol. Cache for reuse across move + quality
        # metric computation. Need ≥ 25 bars for a 20-day average volume
        # calculation, so we pad to that even if lookback_days is tight.
        bars_pad = max(lookback_days + 3, 25)
        bars_cache: dict[str, list] = {}
        for sym in all_syms:
            try:
                bars = self.market.get_ohlcv(sym, lookback_days=bars_pad)
            except Exception:
                continue
            if bars and len(bars) >= 2:
                bars_cache[sym] = bars

        # Per-symbol window return.
        symbol_moves: dict[str, float] = {}
        for sym, bars in bars_cache.items():
            window = bars[-(lookback_days + 1) :] if len(bars) > lookback_days else bars
            if len(window) < 2:
                continue
            start_close = getattr(window[0], "close", 0) or 0
            end_close = getattr(window[-1], "close", 0) or 0
            if start_close <= 0:
                continue
            move_pct = (end_close - start_close) / start_close * 100.0
            symbol_moves[sym] = round(move_pct, 2)

        candidates = {s: m for s, m in symbol_moves.items() if abs(m) >= move_threshold_pct}
        if not candidates:
            return []

        # Pre-compute signal maps once (not per-symbol): cheap vs. re-running
        # DB/file scans inside the loop.
        held_set = self._missed_ops_held_set(lookback_days, current_position_symbols or set())
        tech_map = self._missed_ops_tech_signal(lookback_days)
        news_map = self._missed_ops_news_signal(lookback_days)
        theme_map = self._missed_ops_theme_tags(lookback_days)
        earnings_map = self._missed_ops_earnings_signal(session_earnings)
        macro_sector_map = self._missed_ops_macro_sector_map()

        snapshots: list = []
        for sym, move_pct in candidates.items():
            if sym in universe_set and sym in top_mover_syms:
                source = "both"
            elif sym in top_mover_syms:
                source = "top_mover"
            else:
                source = "universe"

            bars = bars_cache.get(sym) or []
            avg_dvol_m, vol_conf_ratio, single_day_conc = _missed_ops_quality_metrics(
                bars,
                lookback_days,
            )

            # Liquidity pre-filter: thin TOP-MOVER-only symbols drop out here.
            # Universe symbols bypass — they're already curated for quality.
            if source == "top_mover" and avg_dvol_m is not None and avg_dvol_m < min_top_mover_dollar_volume_m:
                logger.debug(
                    "missed_ops: dropping thin top-mover %s (avg $vol %.1fM < %.1fM)",
                    sym,
                    avg_dvol_m,
                    min_top_mover_dollar_volume_m,
                )
                continue

            ta_rating, ta_date = tech_map.get(sym, (None, None))
            had_ta = ta_rating in ("buy", "strong_buy")
            news_headline = news_map.get(sym)
            earnings_signal = earnings_map.get(sym)

            sector_stance = "unknown"
            try:
                from src.sector_reference import _get_sector

                sector = _get_sector(sym) or ""
            except Exception:
                sector = ""
            if sector and sector in macro_sector_map:
                sector_stance = macro_sector_map[sector]

            # Valuation (done per-candidate after threshold filter → only
            # ~5-15 yfinance calls, not 90+). Defaults to all-None on
            # error / ETF / data gap.
            trailing_pe = None
            forward_pe = None
            ps_ratio = None
            try:
                val_info = self.market.get_valuation_metrics(sym) or {}
                trailing_pe = val_info.get("trailing_pe")
                forward_pe = val_info.get("forward_pe")
                ps_ratio = val_info.get("ps_ratio")
            except Exception as exc:
                logger.debug(
                    "missed_ops valuation fetch failed for %s: %s",
                    sym,
                    exc,
                )
            valuation_signal = _valuation_signal_from(forward_pe)

            # Bidirectional opportunity framing: a DOWN move with an
            # intact fundamental signal is the classic value-dip the
            # medium-long-term investor wants to catch. Flag it at the
            # snapshot level so the evening LLM's value_entry_missed
            # classification is grounded, not just vibes.
            has_fundamental_signal = news_headline is not None or earnings_signal is not None
            value_entry_candidate = move_pct <= -8.0 and has_fundamental_signal

            snapshots.append(
                MissedOpportunitySnapshot(
                    symbol=sym,
                    move_pct=move_pct,
                    window_days=lookback_days,
                    held_during_window=(sym in held_set),
                    had_ta_signal=had_ta,
                    had_news_signal=(news_headline is not None),
                    had_earnings_signal=(earnings_signal is not None),
                    source=source,
                    last_ta_rating=ta_rating,
                    last_ta_date=ta_date,
                    last_news_headline=news_headline,
                    theme_tags=theme_map.get(sym, [])[:4],
                    recent_earnings_signal=earnings_signal,
                    macro_sector_tailwind=sector_stance,  # type: ignore[arg-type]
                    avg_dollar_volume_20d_m=avg_dvol_m,
                    volume_confirmation_ratio=vol_conf_ratio,
                    single_day_concentration_pct=single_day_conc,
                    trailing_pe=trailing_pe,
                    forward_pe=forward_pe,
                    ps_ratio=ps_ratio,
                    valuation_signal=valuation_signal,  # type: ignore[arg-type]
                    value_entry_candidate=value_entry_candidate,
                )
            )

        # Drop names we actually held during the window before sorting and
        # truncating. The prompt instructs the LLM to recognize HELD rows
        # and not classify them as "missed", but the LLM-only fence is
        # fragile: a hiccup could let evening emit `value_entry_missed`
        # on a name we literally bought today (held_during_window=True).
        # Pre-filter in Python so even a confused LLM can't surface a
        # held name. Held positions still get full coverage via
        # thesis_health_review — they don't need a "missed" entry.
        snapshots = [s for s in snapshots if not s.held_during_window]

        def _priority_key(s) -> tuple:
            any_signal = s.had_ta_signal or s.had_news_signal or s.had_earnings_signal
            group = 0 if any_signal else 1
            return (group, -abs(s.move_pct))

        snapshots.sort(key=_priority_key)
        return snapshots[:top_n]
