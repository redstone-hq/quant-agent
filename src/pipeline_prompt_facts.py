"""Prompt-facts builders: read-only DB/broker reads turned into LLM context.

Bodies live in src/prompt_facts/ (eight parts plus the held review seat);
this mixin keeps same-named thin shims, built per call so a collaborator swapped after
construction is what the body sees. The module-level names below are re-exported unchanged for importers and patchers
of this module (the ONE mirror block for this module).

Step 1 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210). Moved verbatim out of
`src/pipeline.py` as a mixin, so `TradingPipeline` keeps every one of these as
its own attribute and every test that patches or calls them is untouched.

The defining property of this module: it places no orders, cancels nothing and
amends no stop. `_handle_ex_dividends` sat in this cluster's line range and does
move live stops, so it is NOT here — it goes to the protection module in step 2
(plan §1 correction, 2026-10-01).

Nothing here may import `src.pipeline`: this module is one of its bases.
"""

import json as _json  # noqa: F401 -- re-exported
import logging
import re  # noqa: F401 -- re-exported
from pathlib import Path  # noqa: F401 -- re-exported

from src.execution.stop_read import read_stop  # noqa: F401 -- re-exported
from src.models import TechAnalysisResult  # noqa: F401 -- re-exported
from src.pipeline_context import PMFacts  # noqa: F401 -- re-exported
from src.pipeline_prompt_facts_pure import (  # noqa: F401  re-exports, see pipeline.py
    _actualize_trade_row,
    _build_macro_tech_alignment,
    _missed_ops_quality_metrics,
    _valuation_signal_from,
)
from src.pipeline_prompt_facts_review import hold_prompt_facts_review
from src.prompt_facts.decisions import PromptDecisions
from src.prompt_facts.heat import PromptHeat
from src.prompt_facts.missed_ops_signals import MissedOpsSignals
from src.prompt_facts.pm_facts import _PM_PROFILE_SYMBOL_CAP, PromptPMFacts  # noqa: F401 -- re-exported
from src.prompt_facts.projected import PromptProjected
from src.prompt_facts.watchlist import PromptWatchlist
from src.quantities import avg_dollar_volume, dollar_volumes  # noqa: F401 -- re-exported
from src.risk.metrics import unrealized_pnl_pct  # noqa: F401 -- re-exported
from src.risk.rules import peak_to_trough_pct, position_weight_pct  # noqa: F401 -- re-exported
from src.trading_calendar import et_today, session_date_key  # noqa: F401 -- re-exported

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class PromptExposure:
    """The correlation matrix and the live stop map; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        config=None,
        market=None,
        db=None,
        broker=None,
    ) -> None:
        self.config = config
        self.market = market
        self.db = db
        self.broker = broker

    def _ensure_correlation_matrix(self, ctx, positions) -> dict:
        """Build the run's correlation matrix once, memoized on `ctx`.

        It used to be built inside `RiskStage`, which runs AFTER the Portfolio
        Manager has already chosen — so the PM's prompt could tell it to "avoid
        stacking highly correlated positions" while the only correlation data
        in the system was computed too late to inform that choice (audit §1.2).
        Building it here, from the DecisionStage side, lets PM see the clusters
        BEFORE it decides, and RiskStage reuses the same matrix rather than
        paying for a second one — the deterministic cluster check must judge
        PM against the numbers PM was actually shown.
        """
        cached = getattr(ctx, "correlation_matrix", None)
        if cached:
            return cached
        try:
            from src.data.correlation import build_correlation_matrix

            # THE CORRELATION WINDOW (board item 148), recorded honestly.
            # The bars that feed this matrix span `trading.lookback_days`
            # (deployed 1800 ≈ 5 trading years, config/settings.yaml) — the
            # SAME history fetched for MA200 and every other indicator, reused
            # here rather than chosen for correlation. It has NO correlation-
            # specific derivation: settings.yaml records the 320→1800 raise as
            # "purely for structure" (deterministic support/resistance), and
            # `build_correlation_matrix` needs only 20 overlapping daily returns
            # (`df.corr(min_periods=20)`) over pairwise-complete observations, so
            # the extra ~1,780 bars add older returns that may straddle regime
            # changes rather than sharpen a cluster estimate. The window moved
            # 120d → 5y silently on the switch to `trading.lookback_days`; this
            # comment is the reason that was never recorded — it is INHERITED
            # from the structural-level fetch, not justified for clustering.
            # It is not a ledgered number: `trading.lookback_days` carries no
            # numeric default (`Field(ge=1)` in src/config.py), so it is not a
            # definition site the number-ledger scanner can attach an entry to;
            # the 0.7 cutoff that used to sit beside it is GONE (item 186,
            # 2026-09-30): clusters are now read from the correlation
            # geometry itself, so the window is the only unjustified input
            # left on this path.
            pool_bars = dict(ctx.symbols_bars)
            for p in positions:
                if p.symbol not in pool_bars:
                    pool_bars[p.symbol] = (
                        self.market.get_ohlcv(
                            p.symbol,
                            self.config.trading.lookback_days,
                        )
                        or []
                    )
            matrix = build_correlation_matrix(pool_bars) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to build correlation matrix: %s (continuing without)", e)
            matrix = {}
        ctx.correlation_matrix = matrix
        return matrix

    def _build_stop_map(self, positions) -> tuple[dict[str, float], dict[str, float], set[str]]:
        """`(live_stops, initial_stops, unreadable)` keyed by symbol.

        Live stops are broker truth (already trailed). Initial stops come from
        the last executed BUY row and are what an R-multiple's denominator must
        use — the bet that was actually made, not the one it was ratcheted to.
        A symbol missing from `live_stops` and not in `unreadable` is genuinely unprotected and
        `portfolio_heat` charges it at full notional; never substitute the BUY
        row's stop for a missing broker stop, because that would report
        protection the account does not have.
        """
        live_stops: dict[str, float] = {}
        initial_stops: dict[str, float] = {}
        unreadable: set[str] = set()
        for p in positions:
            sym = p.symbol
            try:
                self._one_stop(p, sym, live_stops, initial_stops, unreadable)
            except Exception as e:  # noqa: BLE001 — one holding degrades, never the whole book
                # Defect 2026-10-09: one raise here used to lose the whole
                # book's risk and skip the 25% ceiling. Unreadable → this
                # holding is charged at full market value by the heat roll-up.
                logger.error("stop map: %s stop could not be read (%s) — charged at full market value", sym, e)
                live_stops.pop(sym, None)
                unreadable.add(sym)
        return live_stops, initial_stops, unreadable

    def _one_stop(self, p, sym, live_stops, initial_stops, unreadable) -> None:
        """One holding's live and initial stop, written into the caller's maps."""
        _live_read = read_stop(self.broker, sym, db=self.db, context="prompt stop map")
        unreadable.update([sym] if _live_read.unreadable else [])
        if _live_read.found:
            live_stops[sym] = _live_read.price
        from src.execution.stop_records import recorded_initial_stop

        try:
            qty = float(getattr(p, "qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            opening = "SHORT" if qty < 0 else "BUY"
            buy = self.db.get_symbol_last_buy(sym, action=opening)
        except Exception as e:  # noqa: BLE001
            logger.warning("stop map: last-buy lookup failed for %s: %s", sym, e)
            buy = None
        initial = recorded_initial_stop(buy)
        if initial > 0:
            initial_stops[sym] = initial


class PromptHistory:
    """Position history, weekly narrative, macro trajectory and active state changes; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        db=None,
        tech_store=None,
        macro_store=None,
        news_store=None,
    ) -> None:
        self.db = db
        self.tech_store = tech_store
        self.macro_store = macro_store
        self.news_store = news_store

    def _build_position_history(self, positions) -> dict[str, dict]:
        """L2 memory: for each held symbol, entry context + Tech rating trajectory.

        PM uses this to anchor 'when did I buy + why' and recognize when a fresh
        setup has been maturing vs stuck vs invalidated.
        """
        from datetime import date as _date
        from src.execution.stop_records import recorded_initial_stop

        out: dict[str, dict] = {}
        today = et_today()
        for p in positions:
            sym = p.symbol
            entry = None
            try:
                try:
                    qty = float(getattr(p, "qty", 0) or 0)
                except (TypeError, ValueError):
                    qty = 0.0
                opening = "SHORT" if qty < 0 else "BUY"
                entry = self.db.get_symbol_last_buy(sym, action=opening)
            except Exception as e:
                logger.warning("position_history: last_buy lookup failed for %s: %s", sym, e)

            entry_date_str = None
            days_held: int | None = None
            if entry and entry.get("timestamp"):
                try:
                    ts = entry["timestamp"]
                    entry_date = _date.fromisoformat(ts[:10]) if isinstance(ts, str) else None
                    if entry_date is not None:
                        entry_date_str = str(entry_date)
                        days_held = max(0, (today - entry_date).days)
                except (ValueError, TypeError):
                    pass

            try:
                tech_history = self.tech_store.get_history(sym, days=7)
            except Exception as e:
                logger.warning("position_history: tech history failed for %s: %s", sym, e)
                tech_history = []

            out[sym] = {
                "entry_date": entry_date_str,
                "entry_price": entry.get("price") if entry else None,
                "entry_reasoning": (entry.get("reasoning") or "")[:280] if entry else "",
                # Real, untruncated falsifier condition — see
                # TradeDecision.thesis_invalid_if in models.py. Carried
                # ALONGSIDE entry_reasoning (never a replacement for it):
                # the embedded "(invalid if: ...)"/"(thesis_invalid_if: ...)"
                # text above is truncated at 280 chars here (and 500 chars
                # upstream in the constructor), which could silently cut off
                # a long condition. This column is None for legacy rows
                # written before the trades.thesis_invalid_if column existed.
                "thesis_invalid_if": entry.get("thesis_invalid_if") if entry else None,
                "days_held": days_held,
                "tech_history": tech_history,
                # The stop recorded at entry — RiskStage's structural
                # holding-discipline check (spec item 25) reads this to find
                # the level backing it. Deliberately the frozen entry-time
                # stop (`initial_stop_loss`), not the live `stop_loss` a
                # trail may have since written back, and not the live
                # broker stop: same "the bet that was actually made"
                # reasoning `_build_position_facts.initial_stop` documents.
                "stop_loss": (recorded_initial_stop(entry) or None) if entry else None,
            }
        return out

    def _build_weekly_narrative(self) -> str:
        """L3a memory: last 7 evenings' daily_summary + daily_pnl, compact."""
        try:
            insights = self.db.get_recent_insights(limit=7)
        except Exception as e:
            logger.warning("weekly_narrative: insights fetch failed: %s", e)
            insights = []
        if not insights:
            return ""
        try:
            pnl_rows = self.db.get_daily_pnl(limit=14)
        except Exception:
            pnl_rows = []
        pnl_by_date = {r["date"]: r for r in pnl_rows}
        lines = []
        # insights come newest-first; display oldest→newest so the "arc" reads naturally
        for row in reversed(insights):
            d = row.get("date", "?")
            summary = (row.get("tomorrow_outlook") or row.get("lessons") or "").strip()
            if len(summary) > 220:
                summary = summary[:217] + "..."
            pnl = pnl_by_date.get(d) or {}
            ret = pnl.get("daily_return_pct")
            ret_str = f"{ret:+.2f}%" if isinstance(ret, (int, float)) else "n/a"
            risk = row.get("risk_rating", "?")
            lines.append(f"- {d}: {ret_str} ({risk}) — {summary}")
        return "\n".join(lines)

    def _build_macro_trajectory(self) -> str:
        """L3b memory: last 7 days of macro regime / confidence / equity outlook.

        No invested target: macro stopped setting one with the owner's
        fully-invested mandate (2026-09-17). Older snapshots still carry
        `position_guidance.target_invested_pct`; it is deliberately not read.
        """
        try:
            history = self.macro_store.load_history(days=7)
        except Exception as e:
            logger.warning("macro_trajectory: load_history failed: %s", e)
            history = []
        if not history:
            return ""
        lines = []
        for snap in history:
            d = snap.get("date", "?")
            regime = snap.get("regime", "?")
            conf = snap.get("confidence", "?")
            outlook = snap.get("equity_outlook") or "?"
            lines.append(f"- {d}: {regime} ({conf}) → outlook {outlook}")
        return "\n".join(lines)

    def _build_active_state_changes(self) -> str:
        """L3c memory: HIGH-conviction state_changes from the last 14 days, deduped."""
        try:
            changes = self.news_store.recent_state_changes(lookback_days=14, limit=8)
        except Exception as e:
            logger.warning("active_state_changes: news_store failed: %s", e)
            changes = []
        if not changes:
            return ""
        lines = []
        for ch in changes:
            d = ch.get("first_seen_date", "?")
            event = (ch.get("event") or "")[:160]
            symbols = ch.get("affected_symbols") or []
            # Phase 13 catalyst-gate fix: render each symbol's direction
            # inline as `SYMBOL(direction)` so `PortfolioManagerAgent.
            # _state_change_symbols_by_date` can parse it back out — this
            # is a round-trip over a format this repo owns end to end
            # (same discipline as the rest of this block). A symbol with
            # no recorded `symbol_direction` (older persisted reports
            # predating this field, or the news analyst genuinely
            # omitting one) renders as `(unknown)`, which the PM-side
            # parser treats as not qualifying — fail closed, never an
            # upgrade to "assume it's good news."
            directions = ch.get("symbol_direction") or {}
            if symbols:
                syms = ", ".join(
                    f"{s.strip().upper()}({directions.get(s.strip().upper(), 'unknown')})" for s in symbols[:6]
                )
            else:
                syms = "—"
            lines.append(f"- [{d}] {event} → {syms}")
        return "\n".join(lines)


class PromptPositionFacts:
    """The per-position facts block; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        db=None,
        broker=None,
        atr_for_symbol=None,
    ) -> None:
        self.db = db
        self.broker = broker
        self._atr_for_symbol = atr_for_symbol  # the host's ATR reader, handed in

    def _build_position_facts(self, positions, morning_trades, total_value):
        """Deterministic per-position metrics surfaced to the reviewer.

        Python does the math (progress %, pace, distance-to-stop/target,
        winner flags) so the LLM sees clean numbers and just interprets
        them. Prevents hallucination of percentages.
        """
        # Morning opening-row lookup by symbol for stop/target/days_held.
        # BUY and SHORT are separate: a leftover purchase on the same
        # ticker is not the short's entry.
        buy_rows: dict[str, dict] = {}
        short_rows: dict[str, dict] = {}
        for t in morning_trades or []:
            sym = t.get("symbol")
            act = (t.get("action") or "").upper()
            if not sym or act not in ("BUY", "SHORT"):
                continue
            bucket = short_rows if act == "SHORT" else buy_rows
            if sym not in bucket:
                bucket[sym] = t

        facts: dict[str, dict] = {}
        for p in positions:
            sym = p.symbol
            entry = p.avg_entry
            cur = p.current_price

            # Find the last executed opening row for this symbol to derive
            # target/stop/days_held. A short must read the SHORT row, not a
            # leftover BUY on the same ticker. Falls back to the morning
            # row of the same side, then the matching last-open lookup.
            if p.qty < 0:
                buy = short_rows.get(sym)
                if not buy:
                    try:
                        buy = self.db.get_symbol_last_buy(sym, action="SHORT")
                    except Exception:
                        buy = None
            else:
                buy = buy_rows.get(sym)
                if not buy:
                    try:
                        buy = self.db.get_symbol_last_buy(sym)
                    except Exception:
                        buy = None

            stop_loss = float((buy or {}).get("stop_loss") or 0)
            # The LIVE target — re-derived if a structural event has since
            # triggered `src.risk.target_revision`. Used for
            # `distance_to_target_pct` and for display, and NEVER as the
            # denominator of progress; see `progress_target` below.
            take_profit = float((buy or {}).get("take_profit") or 0)
            # The ENTRY target, frozen as `initial_take_profit` at insert.
            #
            # THIS, not `take_profit`, is the denominator of
            # `thesis_progress_pct` and therefore of `pace`. The target is
            # the denominator, so RAISING it mechanically LOWERS progress and
            # LOWERS pace — and both are in
            # `src.risk.exit_guard._HIGHER_IS_BETTER`. Measured against the
            # live target, a revision on good news (the name gaps through the
            # resistance the target sat on, the target is re-derived higher)
            # would show up in `MetricDeltas.worsened`, which clears
            # `net_improved`, which switches OFF `veto_contradicted_exit` —
            # so the position would instantly look less progressed and
            # slower than an hour earlier, and a "this position is stalling"
            # SELL that was previously blocked would go through. A machine
            # for making winners look stalled and then selling them.
            #
            # Pinning the denominator removes that by construction rather
            # than by a special case in the guard: a revision cannot move
            # either metric at all, so it cannot appear as a deterioration.
            # Same reasoning as the pinned horizon two paragraphs down — a
            # yardstick that moves measures nothing.
            #
            # Legacy rows that predate the column fall back to the live
            # target, which for them IS the entry target: `take_profit` was
            # only ever written by `insert_trade` before the revision path
            # existed (see the `initial_take_profit` migration in
            # `src/storage/db.py`), and a row with no revision has nothing
            # to diverge from.
            progress_target = float((buy or {}).get("initial_take_profit") or take_profit or 0)
            # The ENTRY stop, frozen as `initial_stop_loss` on first
            # write-back. R-multiple's denominator is the bet that was
            # actually made, not the level a trail later ratcheted it to
            # (audit §1.4).
            from src.execution.stop_records import recorded_initial_stop

            initial_stop = recorded_initial_stop(buy)

            # RC1: after any TRAIL_STOP the BUY row's stop is stale-WIDE —
            # the reviewer would see a fat distance_to_stop and keep
            # ratcheting. Prefer live broker truth; fall back to the BUY row.
            _ls_read = read_stop(self.broker, sym, db=self.db, context="prompt position facts")
            if _ls_read.found:
                stop_loss = _ls_read.price

            # days_held — from BUY timestamp; fall back to None.
            #
            # sessions_held is the holiday-aware companion count (item 165:
            # the broker's real market calendar via `broker.trading_sessions_held`,
            # not the plain Mon-Fri weekday counter in
            # `trading_calendar.trading_sessions_held`, which overstates by
            # one session per market holiday crossed) — the noise-band
            # scaling below needs TRADING SESSIONS, not calendar days, per
            # the 2026-09-04 audit follow-up.
            days_held = None
            sessions_held = None
            buy_ts = (buy or {}).get("timestamp")
            if buy_ts:
                try:
                    from src.trading_calendar import to_et
                    from datetime import datetime as _dt

                    dt = (
                        _dt.fromisoformat(buy_ts.replace("Z", "+00:00"))
                        if "T" in buy_ts
                        else _dt.strptime(buy_ts, "%Y-%m-%d %H:%M:%S")
                    )
                    entry_date = to_et(dt).date()
                    days_held = (et_today() - entry_date).days
                    days_held = max(0, days_held)
                    sessions_held = self.broker.trading_sessions_held(entry_date, et_today())
                except Exception:
                    days_held = None
                    sessions_held = None

            # Phase 3.1 — the thesis horizon and setup type PINNED AT ENTRY.
            # Read from the BUY row, never recomputed. NULL for positions
            # opened before this landed, and for sweep/resume-lane buys with
            # no analysis; those get no pace figure rather than a fabricated
            # one.
            pinned_horizon = (buy or {}).get("expected_horizon_sessions")
            try:
                pinned_horizon = int(pinned_horizon) if pinned_horizon else None
            except (TypeError, ValueError):
                pinned_horizon = None
            setup_type = (buy or {}).get("setup_type") or None
            # Item 82: the MEASURED half of the SAME verdict, pinned at entry
            # alongside `setup_type` (stored 0/1/NULL). Present → this path
            # reaches construction's OWN breakout verdict, not a label-only
            # approximation of it; NULL (legacy row, or a pre-item-82 entry)
            # → `is_trend_trade` falls back to the label alone, exactly as
            # this code did before the column existed. See
            # `src.risk.constants.is_trend_trade`.
            _sc_raw = (buy or {}).get("structural_ceiling")
            structural_ceiling = None if _sc_raw is None else bool(_sc_raw)

            # Progress: 0 at entry, 100 at target, >100 beyond target.
            #
            # DISABLED for breakout ("Type B") setups. A breakout's target is a
            # measured-move reference, not a level anyone is defending — there
            # is no overhead structure for price to progress TOWARD, so
            # "progress" against it measures nothing and "pace" against that
            # nothing is worse. Breakouts are managed by trailing instead
            # (spec Phase 3.7). The breakout verdict is pinned at entry: the
            # analyst's `setup_type` OR the constructor's measured
            # `structural_ceiling` — either sufficient, see `is_trend_trade`.
            # ALSO DISABLED when the whole distance from entry to the target
            # is smaller than one ordinary session's range (2026-09-30).
            #
            # Same defect as the breakout case directly above, reached by a
            # different road. `progress_target - entry` is the denominator,
            # so when the target sits on a wall a fraction of an ATR
            # overhead, `progress_pct` measures the denominator's smallness
            # and not the thesis. META's 2026-09-21 add has $2.00 of room
            # against a $21.22 ATR: a third of one ATR reads as 354%
            # progress, and `target_breach_flag` (>150%) would render
            # "TARGET_BREACH" into the position reviewer's prompt — a seat
            # that can answer SELL or REDUCE. `pace` shares the denominator
            # and sits in `exit_guard._HIGHER_IS_BETTER`, so it moves
            # `MetricDeltas.net_improved` and with it
            # `veto_contradicted_exit`.
            #
            # The owner ruled on 2026-09-30 that a computed target is never
            # an exit trigger. A warning glyph driven off that target is
            # that trigger wearing a different hat, and on a sub-noise
            # denominator it fires on movement that means nothing.
            #
            # This is not a new threshold: it is `MIN_TARGET_ATR_MULTIPLE`,
            # the same noise floor `derive_structural_target` labels the
            # target with, read against the live ATR rather than pinned.
            # Live is correct here and deliberate — the question is whether
            # today's movement can be read as progress, which is a question
            # about today's volatility. It also self-heals the three rows
            # already carrying a pre-fix target.
            from src.risk.constants import is_trend_trade
            from src.data.levels import MIN_TARGET_ATR_MULTIPLE

            live_atr = self._atr_for_symbol(sym)
            target_room_is_noise = bool(
                progress_target
                and entry
                and live_atr
                and live_atr > 0
                and abs(progress_target - entry) < live_atr * MIN_TARGET_ATR_MULTIPLE
            )
            progress_pct = None
            pace = None
            pace_status = "unavailable"
            if is_trend_trade(setup_type, structural_ceiling=structural_ceiling):
                pace_status = "n/a_breakout"
            elif target_room_is_noise:
                pace_status = "n/a_target_inside_noise"
            else:
                if progress_target and entry and progress_target != entry:
                    progress_pct = (cur - entry) / (progress_target - entry) * 100

                # Pace against the horizon the ANALYST pinned at entry.
                #
                # This replaces `days_held / avg_hold_days`, where avg_hold_days
                # came from the system's own rolling 30-day realized-trade
                # calibration (~2.0 days in practice). That made pace a
                # feedback loop: every early sale shrank the average, which made
                # every surviving position look stalled, which drove more early
                # sales. A self-tightening noose, and the single largest
                # identified P&L defect in the system. A trade's expected
                # horizon must never be derived from the system's own past
                # behaviour.
                #
                # Board item 91: `pinned_horizon` (`expected_horizon_sessions`)
                # is denominated in TRADING SESSIONS, so both sides of this
                # division must be — `sessions_held`, never the calendar-day
                # `days_held`. A weekend adds two calendar days and zero
                # sessions; dividing by calendar days made every position look
                # slower than it is, worst on the short horizons this desk
                # trades, and worse across a holiday weekend. `sessions_held`
                # is the same weekend-aware count the noise-band scaling above
                # already uses (`trading_calendar.trading_sessions_held`) —
                # no new number, just the one already computed above.
                # Board item 165 (owner ruling 2026-09-25): there is NO
                # elapsed-time floor before pace is judged. The old
                # `sessions_held < max(1, pinned_horizon / 3)` "too_early" gate
                # was a made-up clock stacked on a guessed horizon; the desk
                # reassesses every review from the live instrument, so pace is
                # computed and surfaced from the first review whenever the
                # inputs exist. Early pace is naturally extreme (a tiny
                # time_fraction), which the reviewer reads as context — it is
                # never on its own a reason to exit (see the prompt).
                if progress_pct is None or not pinned_horizon or sessions_held is None:
                    pace_status = "unavailable_no_pinned_horizon"
                else:
                    time_fraction = sessions_held / pinned_horizon
                    if time_fraction > 0:
                        pace = progress_pct / (time_fraction * 100)
                        pace_status = "measured"

            # Distance-to-stop / distance-to-target as % of current price.
            #
            # `distance_to_stop_pct` MUST be side-aware. A LONG's stop sits
            # BELOW price, so `(cur - stop_loss)` is the room left and is
            # positive while the position is alive. A SHORT's stop sits
            # ABOVE price, so that same expression is NEGATIVE, and it moves
            # the WRONG way: it gets MORE negative (looks worse under
            # `_HIGHER_IS_BETTER`) as the price falls further from the stop
            # — i.e. as the position gets safer. Mirror the numerator for a
            # short (`p.qty < 0`) so the metric means the same thing on both
            # sides: positive, and falling as the stop gets closer. See
            # `src/risk/exit_guard.py::_HIGHER_IS_BETTER`, which trusts this
            # value to already be direction-corrected.
            dist_stop_pct = None
            dist_target_pct = None
            if stop_loss and cur > 0:
                dist_stop_pct = ((stop_loss - cur) if p.qty < 0 else (cur - stop_loss)) / cur * 100
            if take_profit and cur > 0:
                dist_target_pct = (take_profit - cur) / cur * 100

            # GROSS-leverage weight — the one definition (see
            # `src.risk.rules.weight_pct_of`). Raw here meant the reviewer
            # was shown a 3x fund at a third of the weight the engine caps
            # it at, and the drift flag below never fired on one.
            weight_pct = position_weight_pct(p, total_value)

            # Winner flags. `unrealized_pnl_pct` divides by |entry x qty|;
            # a short's negative qty otherwise flips the sign and feeds the
            # parabolic flag the wrong side. None = unknowable, which
            # is not a flag either way.
            pnl_pct = unrealized_pnl_pct(p)
            parabolic_flag = pnl_pct is not None and pnl_pct >= 15 and days_held is not None and days_held < 3

            # Vol-unit context so the reviewer reasons about stop distance
            # in ATRs, not raw % (a 3% gap is roomy for KO, suicidal for
            # RKLB). None when bars are unavailable — the prompt treats
            # missing as "unknown", never as zero.
            atr = self._atr_for_symbol(sym)
            atr_pct = round(atr / cur * 100, 2) if (atr and cur > 0) else None
            stop_distance_atrs = None
            if atr and stop_loss and cur > stop_loss:
                stop_distance_atrs = round((cur - stop_loss) / atr, 2)

            # R-multiple (audit §1.4) — profit in units of the risk taken.
            # `thesis_progress_pct` measures distance to TARGET, a different
            # question that does not normalise for how much was risked: a name
            # 20% of the way to a distant target may be +2R or +0.3R, and only
            # the second is a reason to leave it alone.
            from src.risk.metrics import position_risk as _position_risk

            risk = _position_risk(
                symbol=sym,
                qty=p.qty,
                entry=entry,
                current_price=cur,
                stop=stop_loss or None,
                initial_stop=initial_stop or None,
            )

            facts[sym] = {
                "days_held": days_held,
                "sessions_held": sessions_held,
                "expected_horizon_sessions": pinned_horizon,
                "setup_type": setup_type,
                "pace_status": pace_status,
                "r_multiple": risk.r_multiple,
                "initial_stop": initial_stop or None,
                "risk_released": risk.risk_released,
                "open_risk_dollars": risk.open_risk_dollars,
                "thesis_progress_pct": progress_pct,
                "pace": pace,
                "distance_to_stop_pct": dist_stop_pct,
                "distance_to_target_pct": dist_target_pct,
                # Provenance for `distance_to_stop_pct`, not metrics of
                # their own: that metric is a function of BOTH terms, so
                # without them a rise caused by the stop being widened is
                # indistinguishable from a rise caused by the price moving
                # away. It read as "(improved)" on real 2026-09-01
                # snapshots for V, CMCSA and DIS while all three were
                # deteriorating. See `exit_guard._STOP_DEPENDENT_METRIC`.
                "stop_loss": stop_loss or None,
                "stop_status": "live stop COULD NOT BE READ" if _ls_read.unreadable else None,
                "current_price": cur if cur > 0 else None,
                # Side, so the provenance recomputation in
                # `exit_guard.MetricDeltas._distance_move_is_price_driven`
                # can mirror the SAME formula this block uses above
                # (`dist_stop_pct`) rather than assume every position is a
                # long. Never scored — not a metric, just qty's sign.
                "qty": p.qty,
                "weight_pct": weight_pct,
                "parabolic_flag": parabolic_flag,
                "atr_pct": atr_pct,
                "stop_distance_atrs": stop_distance_atrs,
                # Both targets, named for what they are. `take_profit` is the
                # live (possibly re-derived) number; `entry_take_profit` is
                # the pinned entry derivation that `thesis_progress_pct` and
                # `pace` above are measured against. Surfaced separately so
                # neither the reviewer nor the cockpit has to guess which
                # number a progress figure came from.
                "take_profit": take_profit or None,
                "entry_take_profit": progress_target or None,
                "target_revised": bool(
                    take_profit and progress_target and round(take_profit, 2) != round(progress_target, 2)
                ),
            }
        return facts


@hold_prompt_facts_review  # holds review part
class PromptFactsMixin:
    """Read-only prompt-context builders mixed into `TradingPipeline`; every body lives on a part under src/prompt_facts/.

    Each `_prompt_*` builder reads the host's collaborators at call time. Cross-family reads (pm facts -> heat and
    history; heat -> stop map) get the host's shim, never a body the part owns, so no recursion guard is needed.
    The projected part holds no sector cache: the resolved sector map is written onto the RunContext handed to it."""

    # Free-standing helpers now live in src/pipeline_prompt_facts_pure.py;
    # re-bound here so `self._x(...)` / `PromptFactsMixin._x` keep working.
    _actualize_trade_row = staticmethod(_actualize_trade_row)
    _build_macro_tech_alignment = staticmethod(_build_macro_tech_alignment)

    def _prompt_history(self) -> PromptHistory:
        return PromptHistory(
            db=getattr(self, "db", None),
            tech_store=getattr(self, "tech_store", None),
            macro_store=getattr(self, "macro_store", None),
            news_store=getattr(self, "news_store", None),
        )

    def _prompt_decisions(self) -> PromptDecisions:
        return PromptDecisions(
            db=getattr(self, "db", None),
            parse_logged_agent_response=getattr(self, "_parse_logged_agent_response", None),
        )

    def _prompt_projected(self) -> PromptProjected:
        return PromptProjected(
            portfolio_constructor=getattr(self, "portfolio_constructor", None),
            risk_engine=getattr(self, "risk_engine", None),
        )

    def _prompt_watchlist(self) -> PromptWatchlist:
        return PromptWatchlist(db=getattr(self, "db", None))

    def _prompt_exposure(self) -> PromptExposure:
        return PromptExposure(
            config=getattr(self, "config", None),
            market=getattr(self, "market", None),
            db=getattr(self, "db", None),
            broker=getattr(self, "broker", None),
        )

    def _prompt_heat(self) -> PromptHeat:
        return PromptHeat(
            sweeper=getattr(self, "_sweeper", None), build_stop_map=getattr(self, "_build_stop_map", None)
        )

    def _prompt_pm_facts(self) -> PromptPMFacts:
        return PromptPMFacts(
            db=getattr(self, "db", None),
            config=getattr(self, "config", None),
            parse_logged_agent_response=getattr(self, "_parse_logged_agent_response", None),
            build_portfolio_heat=getattr(self, "_build_portfolio_heat", None),
            build_position_history=getattr(self, "_build_position_history", None),
        )

    def _prompt_position_facts(self) -> PromptPositionFacts:
        return PromptPositionFacts(
            db=getattr(self, "db", None),
            broker=getattr(self, "broker", None),
            atr_for_symbol=getattr(self, "_atr_for_symbol", None),
        )

    def _build_thesis_health_context(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._build_thesis_health_context(*args, **kwargs)

    def _missed_ops_signals(self) -> MissedOpsSignals:
        """Standalone signal helpers for the missed-ops digest and thesis-health review
        (bodies moved to src/prompt_facts/missed_ops_signals.py). Built per call so a
        collaborator swapped after construction is what the body sees. No lifted body
        is passed back in: none of these helpers calls another, so there is nothing
        for the shim to overwrite (see src/cost_circuit/parts/shim_guard.py)."""
        return MissedOpsSignals(
            db=getattr(self, "db", None),
            news_store=getattr(self, "news_store", None),
            earnings_provider=getattr(self, "earnings_provider", None),
            macro_store=getattr(self, "macro_store", None),
            parse_logged_agent_response=getattr(self, "_parse_logged_agent_response", None),
            broker=getattr(self, "broker", None),
            market=getattr(self, "market", None),
            config=getattr(self, "config", None),
        )

    def _missed_ops_held_set(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._missed_ops_held_set(*args, **kwargs)

    def _missed_ops_tech_signal(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._missed_ops_tech_signal(*args, **kwargs)

    def _missed_ops_news_signal(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._missed_ops_news_signal(*args, **kwargs)

    def _missed_ops_theme_tags(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._missed_ops_theme_tags(*args, **kwargs)

    def _missed_ops_earnings_signal(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._missed_ops_earnings_signal(*args, **kwargs)

    def _missed_ops_macro_sector_map(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._missed_ops_macro_sector_map(*args, **kwargs)

    def _thesis_tech_trajectory_map(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._thesis_tech_trajectory_map(*args, **kwargs)

    def _thesis_news_events_map(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._thesis_news_events_map(*args, **kwargs)

    def _build_missed_opportunities_digest(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/missed_ops_signals.py."""
        return self._missed_ops_signals()._build_missed_opportunities_digest(*args, **kwargs)

    def _build_position_history(self, *args, **kwargs):
        """Thin shim: body lives in the standalone class in this module (it reads the broker seam, so it cannot move out)."""
        return self._prompt_history()._build_position_history(*args, **kwargs)

    def _build_weekly_narrative(self, *args, **kwargs):
        """Thin shim: body lives in the standalone class in this module (it reads the broker seam, so it cannot move out)."""
        return self._prompt_history()._build_weekly_narrative(*args, **kwargs)

    def _build_macro_trajectory(self, *args, **kwargs):
        """Thin shim: body lives in the standalone class in this module (it reads the broker seam, so it cannot move out)."""
        return self._prompt_history()._build_macro_trajectory(*args, **kwargs)

    def _build_active_state_changes(self, *args, **kwargs):
        """Thin shim: body lives in the standalone class in this module (it reads the broker seam, so it cannot move out)."""
        return self._prompt_history()._build_active_state_changes(*args, **kwargs)

    def _build_rm_recent_verdicts(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/decisions.py."""
        return self._prompt_decisions()._build_rm_recent_verdicts(*args, **kwargs)

    def _build_pm_recent_decisions(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/decisions.py."""
        return self._prompt_decisions()._build_pm_recent_decisions(*args, **kwargs)

    def _build_review_metric_deltas(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/decisions.py."""
        return self._prompt_decisions()._build_review_metric_deltas(*args, **kwargs)

    def _build_own_recent_decisions(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/decisions.py."""
        return self._prompt_decisions()._build_own_recent_decisions(*args, **kwargs)

    def _build_projected_portfolio(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/projected.py; needs the caller's `run=` context."""
        return self._prompt_projected()._build_projected_portfolio(*args, **kwargs)

    def _build_watchlist_candidates(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/watchlist.py."""
        return self._prompt_watchlist()._build_watchlist_candidates(*args, **kwargs)

    def _ensure_correlation_matrix(self, *args, **kwargs):
        """Thin shim: body lives in the standalone class in this module (it reads the broker seam, so it cannot move out)."""
        return self._prompt_exposure()._ensure_correlation_matrix(*args, **kwargs)

    def _build_stop_map(self, *args, **kwargs):
        """Thin shim: body lives in the standalone class in this module (it reads the broker seam, so it cannot move out)."""
        return self._prompt_exposure()._build_stop_map(*args, **kwargs)

    def _build_portfolio_heat(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/heat.py."""
        return self._prompt_heat()._build_portfolio_heat(*args, **kwargs)

    def _build_pm_facts(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/pm_facts.py."""
        return self._prompt_pm_facts()._build_pm_facts(*args, **kwargs)

    @staticmethod
    def _log_conviction_outcome_for_operator(stats: dict) -> None:
        """Thin shim: body moved to src/prompt_facts/pm_facts.py."""
        return PromptPMFacts._log_conviction_outcome_for_operator(stats)

    def _build_position_facts(self, *args, **kwargs):
        """Thin shim: body lives in the standalone class in this module (it reads the broker seam, so it cannot move out)."""
        return self._prompt_position_facts()._build_position_facts(*args, **kwargs)
