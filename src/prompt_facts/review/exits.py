"""src.prompt_facts.review.exits -- post-exit reality, the missed-lesson and loss-pit digests.

Bodies moved verbatim from src/pipeline_prompt_facts_review.py (`PromptFactsReviewMixin`),
which keeps same-named thin shims built per call. Every collaborator is an explicit
keyword-only constructor argument, so this builds and runs with no pipeline behind it.
"""

import logging


logger = logging.getLogger(__name__)


class ReviewExits:
    """Post-exit reality, the missed-lesson and loss-pit digests; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        db=None,
        broker=None,
        sweeper=None,
        exit_audit_actions=None,
    ) -> None:
        self.db = db
        self.broker = broker
        self._sweeper = sweeper  # the host's callable, read per use (never snapshotted)
        self._EXIT_AUDIT_ACTIONS = exit_audit_actions

    def _build_post_exit_reality(
        self,
        lookback_days: int = 14,
        min_age_days: int = 2,
        max_symbols: int = 12,
    ) -> dict | None:
        """What actually happened after our recent exits — from the tape.

        For every realized exit in the window (SELL family + filled
        TRAIL_STOPs) at least `min_age_days` old, compare the exit price to
        the live price. Returns None when there's nothing to audit.

        {"n": int, "n_higher_5pct": int, "avg_move_pct": float,
         "worst": [{"symbol", "date", "move_pct"} × ≤3]}   # worst = ran most
        """
        from datetime import datetime as _dt, timedelta, timezone

        from src.trading_calendar import utc_now

        try:
            rows = self.db.get_trades(limit=120)
        except Exception as e:  # noqa: BLE001
            logger.warning("post_exit_reality: trades fetch failed: %s", e)
            return None
        now = utc_now()
        window_start = now - timedelta(days=lookback_days)
        age_cutoff = now - timedelta(days=min_age_days)
        sweeper = self._sweeper()
        sweep_symbol = sweeper.symbol if sweeper is not None else None
        exits: list[dict] = []
        for row in rows:
            action = (row.get("action") or "").upper()
            # Belt on top of the SWEEP_* action exclusion: an emergency
            # liquidation can exit the sweep vehicle under EMERGENCY_SELL —
            # a ~0% T-bill "move" is noise in a decision-quality audit.
            if sweep_symbol is not None and (row.get("symbol") or "") == sweep_symbol:
                continue
            is_exit = (
                action in self._EXIT_AUDIT_ACTIONS
                or action.startswith("PARTIAL_SELL")
                or (action == "TRAIL_STOP" and (row.get("fill_status") or "") == "filled")
            )
            if not is_exit:
                continue
            if action != "TRAIL_STOP" and (row.get("fill_status") or "") not in (
                "filled",
                "submitted",
            ):
                continue
            ts = row.get("timestamp") or ""
            try:
                dt = (
                    _dt.fromisoformat(ts.replace("Z", "+00:00"))
                    if "T" in ts
                    else _dt.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                )
            except (TypeError, ValueError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if not (window_start <= dt <= age_cutoff):
                continue
            exit_px = row.get("fill_price") or row.get("price")
            if not (isinstance(exit_px, (int, float)) and exit_px > 0):
                continue
            exits.append(
                {
                    "symbol": row.get("symbol"),
                    "date": ts[:10],
                    "exit_px": float(exit_px),
                }
            )
        if not exits:
            return None
        # Live prices — one broker call per distinct symbol, capped.
        prices: dict[str, float] = {}
        for sym in list(dict.fromkeys(e["symbol"] for e in exits))[:max_symbols]:
            try:
                px = self.broker.get_latest_price(sym)
            except Exception:  # noqa: BLE001
                px = None
            if isinstance(px, (int, float)) and px > 0:
                prices[sym] = float(px)
        moves: list[dict] = []
        for e in exits:
            cur = prices.get(e["symbol"])
            if cur is None:
                continue
            moves.append(
                {
                    "symbol": e["symbol"],
                    "date": e["date"],
                    "move_pct": round((cur - e["exit_px"]) / e["exit_px"] * 100, 1),
                }
            )
        if not moves:
            return None
        moves.sort(key=lambda m: -m["move_pct"])
        return {
            "n": len(moves),
            "n_higher_5pct": sum(1 for m in moves if m["move_pct"] >= 5.0),
            "avg_move_pct": round(sum(m["move_pct"] for m in moves) / len(moves), 1),
            "worst": moves[:3],
        }

    def _build_recent_missed_lessons(self, lookback_days: int = 14) -> str:
        """PM L3d memory: themes that evening flagged ≥ 2 times as missed.

        Reads `insights.missed_opportunities_json` for the last N days, skips
        the two "not-really-a-miss" categories (noise_rally, risk_disciplined),
        groups by `theme_if_any` (falling back to `symbol` when no theme
        tagged), keeps themes seen on 2+ distinct dates. Output is prose
        PM renders directly — the whole point of this memory layer is PM
        sees "nuclear/power keeps showing up — am I blind to it?" before
        deciding today's positions.

        Empty string when there's nothing worth surfacing — PM's L3d section
        then shows a default "no recurring missed themes" note.
        """
        import json as _json

        try:
            rows = self.db.get_recent_insights(limit=lookback_days + 5)
        except Exception as e:
            logger.warning("recent_missed_lessons: insights fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        # RC4 (2026-07-16): value_entry_missed IS a real, actionable miss —
        # it's the category evening uses for "we identified the entry and
        # didn't take it" (SNDK was flagged 16×, ORCL 7×, and PM never saw
        # any of it because this set filtered them out). Only the two
        # "not-really-a-miss" categories stay excluded.
        real_miss_cats = {
            "trend_timing_miss",
            "theme_blindspot",
            "fundamentals_mispricing",
            "value_entry_missed",
        }
        theme_dates: dict[str, set[str]] = {}
        theme_symbols: dict[str, list[str]] = {}
        theme_lessons: dict[str, str] = {}  # most recent lesson text per theme
        for row in rows[:lookback_days]:
            row_date = row.get("date") or ""
            raw = row.get("missed_opportunities_json")
            if not raw:
                continue
            try:
                items = _json.loads(raw)
            except (TypeError, ValueError) as e:
                # L3d aggregates 14d of missed themes for PM. A single
                # corrupt insights row used to vanish silently from PM's
                # view; surfaces it so a recurring DB corruption pattern
                # is visible in logs instead of the layer just looking
                # "empty" some days.
                logger.warning(
                    "recent_missed_lessons: JSON parse failed for insights row %s: %s",
                    row_date or "?",
                    e,
                )
                continue
            if not isinstance(items, list):
                continue
            for m in items:
                if not isinstance(m, dict):
                    continue
                cat = m.get("miss_category")
                if cat not in real_miss_cats:
                    continue
                theme = (m.get("theme_if_any") or "").strip()
                sym = (m.get("symbol") or "").strip().upper()
                # DUAL grouping keys. RC4 (2026-07-16): theme_if_any is LLM
                # free text that almost never repeats verbatim (45 distinct
                # themes, 0 recurring in the audit window) — keyed ONLY by
                # theme, a symbol missed 16 times (SNDK) diluted into 16
                # one-off "themes" and PM was shown "(no recurring missed
                # themes)" every run. Symbol-keyed counting fixes that;
                # theme-keyed counting is KEPT because cross-symbol theme
                # recurrence (VST + OKLO both "nuclear/power") is a real,
                # distinct signal a symbol key can't see.
                keys = set()
                if sym:
                    keys.add(f"sym:{sym}")
                if theme:
                    keys.add(theme)
                if not keys:
                    continue
                for key in keys:
                    theme_dates.setdefault(key, set()).add(row_date)
                    theme_symbols.setdefault(key, []).append(sym)
                    # Rows are newest-first; first lesson seen is freshest.
                    if key not in theme_lessons:
                        lesson = (m.get("lesson") or "").strip()
                        if lesson:
                            theme_lessons[key] = lesson[:200]

        # Keep themes seen in ≥ 2 distinct EPISODES (audit round 2): the
        # missed-ops digest uses a rolling 5-session window, so one big
        # single-day move re-emits the same miss on ~5 consecutive evenings —
        # "≥2 distinct dates" was auto-satisfied by every one-off spike.
        # Dates within 5 days of the previous date collapse into one episode.
        def _episodes(dates: set[str]) -> int:
            from datetime import date as _d

            parsed = sorted(_d.fromisoformat(x) for x in dates if isinstance(x, str) and len(x) >= 10) if dates else []
            if not parsed:
                return 0
            n = 1
            for a, b in zip(parsed, parsed[1:]):
                if (b - a).days > 5:
                    n += 1
            return n

        # Recurring = ≥2 separated episodes OR ≥2 distinct symbols. The
        # symbol arm keeps the genuine cross-symbol theme case (VST + OKLO
        # both flagged "nuclear/power" on adjacent days = one market episode
        # but a REAL breadth signal), which pure episode-counting would drop.
        recurring = [
            (k, max(_episodes(theme_dates[k]), len({x for x in theme_symbols.get(k, []) if x})))
            for k in theme_dates
            if (_episodes(theme_dates[k]) >= 2 or len({x for x in theme_symbols.get(k, []) if x}) >= 2)
        ]
        if not recurring:
            return ""
        # Sort by occurrence count desc, then key alpha for determinism.
        recurring.sort(key=lambda x: (-x[1], x[0]))
        lines: list[str] = []
        for key, n_days in recurring[:5]:
            syms = theme_symbols.get(key, [])
            uniq = sorted(set(syms))
            sym_tally = ", ".join(f"{s}×{syms.count(s)}" if syms.count(s) > 1 else s for s in uniq[:6])
            lesson = theme_lessons.get(key, "")
            label = key[4:] if key.startswith("sym:") else key
            line = f"- {label}: {n_days} days (symbols: {sym_tally})"
            if lesson:
                line += f' — latest lesson: "{lesson}"'
            lines.append(line)
        return "\n".join(lines)

    def _build_recent_loss_pits(self, lookback_days: int = 14) -> str:
        """PM L3f memory: repeat failure modes from losing BUYs.

        Reads `insights.buy_grades_json` for the last N days, pulls entries
        with `grade="wrong"` and a non-null `loss_root_cause`, groups by
        cause, keeps causes occurring ≥ 2 times. Output is prose PM renders
        directly — lets it see "greed_top_chasing × 3 over 14 days"
        BEFORE deciding today's sizing, not after another wrong entry.

        Empty string when no repeat pattern — PM's L3f section then shows
        a default "no recurring pits" note.
        """
        import json as _json

        try:
            rows = self.db.get_recent_insights(limit=lookback_days + 5)
        except Exception as e:
            logger.warning("recent_loss_pits: insights fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        cause_symbols: dict[str, list[str]] = {}
        cause_move: dict[str, list[float]] = {}
        cause_refs: dict[str, list[str]] = {}
        for row in rows[:lookback_days]:
            raw = row.get("buy_grades_json")
            if not raw:
                continue
            try:
                items = _json.loads(raw)
            except (TypeError, ValueError) as e:
                # L3f aggregates 14d of loss-root-cause patterns. Same
                # silent-drop rationale as L3d above.
                logger.warning(
                    "recent_loss_pits: JSON parse failed for insights row %s: %s",
                    (row.get("date") or "?"),
                    e,
                )
                continue
            if not isinstance(items, list):
                continue
            for g in items:
                if not isinstance(g, dict):
                    continue
                if g.get("grade") != "wrong":
                    continue
                cause = (g.get("loss_root_cause") or "").strip()
                if not cause:
                    continue
                sym = (g.get("symbol") or "").strip().upper()
                move = g.get("pct_move_since_buy")
                ref = (g.get("missed_warning_ref") or "").strip()
                if sym:
                    cause_symbols.setdefault(cause, []).append(sym)
                if isinstance(move, (int, float)):
                    cause_move.setdefault(cause, []).append(float(move))
                if ref:
                    cause_refs.setdefault(cause, []).append(ref[:100])
        repeats = [(c, len(cause_symbols.get(c, []))) for c in cause_symbols if len(cause_symbols.get(c, [])) >= 2]
        if not repeats:
            return ""
        repeats.sort(key=lambda x: (-x[1], x[0]))
        lines: list[str] = []
        for cause, n in repeats[:4]:
            syms = cause_symbols[cause]
            moves = cause_move.get(cause, [])
            detail_bits: list[str] = []
            for i, s in enumerate(syms[:4]):
                m = moves[i] if i < len(moves) else None
                detail_bits.append(f"{s} ({m:+.1f}%)" if m is not None else s)
            line = f"- {cause} × {n}: {', '.join(detail_bits)}"
            refs = cause_refs.get(cause, [])
            if refs and cause == "macro_warning_ignored":
                line += f' — ignored: "{refs[0]}"'
            lines.append(line)
        return "\n".join(lines)
