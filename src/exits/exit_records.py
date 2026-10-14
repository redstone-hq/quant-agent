"""The sell-side records the exit engine reads and writes: today's trims, filed target revisions, recent trail tightenings, exit-review approvals, and the event-risk section fetched for an exit review.

Lifted verbatim out of `ExitEngineMixin` (src/pipeline_exits.py) as a
standalone class: every collaborator is an explicit keyword-only
constructor argument, so it is built and exercised without a pipeline.
The mixin keeps a thin method of the same name that builds this object
and calls it, so every existing caller and patch target is unchanged.
"""

import logging
from datetime import timedelta
from src.sentinel.guarded import record_guarded_pass

#: Logs under `src.pipeline`, as the bodies did before the move;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class ExitRecords:
    """The sell-side records the exit engine reads and writes: today's trims, filed target revisions, recent trail tightenings, exit-review approvals, and the event-risk section fetched for an exit review."""

    def __init__(self, *, trade_executed_or_pending, record_exit_refusal, db, market) -> None:
        self._trade_executed_or_pending = trade_executed_or_pending
        self._record_exit_refusal = record_exit_refusal
        self.db = db
        self.market = market

    def _symbols_already_trimmed_today(self) -> set[str]:
        """Symbols that received a sell-side action earlier today (ET).

        Used by position_reviewer's same-day-trim discipline at midday/close:
        if midday already trimmed AMZN at +12% on TARGET_BREACH, close should
        not trim it AGAIN at +13% on the same flag — that loop produced a
        73% one-day cut on a still-working position (2026-05-04 AMZN 41 →
        21 → 11 shares).

        Sell-side = REDUCE / SELL / TAKE_PROFIT (historical rows only — the
        auto trim was deleted 2026-09-12) / PARTIAL_SELL(...) /
        EMERGENCY_SELL / FORCE_DELEVER, and its short-side mirror COVER /
        EMERGENCY_COVER / PARTIAL_COVER(...) (Stage 3 — a short trimmed at
        midday must be exempt from a second same-flag COVER at close for
        the exact reason a long is). TRAIL_STOP and HOLD do NOT count
        (TRAIL_STOP is stop adjustment, HOLD is no-op).

        Filters out canceled / rejected / expired orders that filled ZERO
        shares — if a SELL was submitted earlier and the broker rejected it,
        the symbol is fair game for re-trying. A PARTIAL fill still blocks:
        those shares left the book, so a second trim today would be the
        double-application this guard exists to prevent. Pending (`submitted`)
        and `filled` rows both block, so we never double-submit on the same
        symbol within one day.
        """
        try:
            rows = self.db.get_trades(today_only=True, limit=200)
        except Exception as exc:
            record_guarded_pass(self.db, "exit_records.symbols_already_trimmed_today", exc)
            logger.warning(
                "_symbols_already_trimmed_today: query failed: %s",
                exc,
            )
            return set()
        sell_actions = {
            "REDUCE",
            "SELL",
            "TAKE_PROFIT",
            "EMERGENCY_SELL",
            "FORCE_DELEVER",
            "COVER",
            "EMERGENCY_COVER",
        }
        out: set[str] = set()
        for r in rows:
            action = (r.get("action") or "").upper()
            # Normalise PARTIAL_SELL(15%) → PARTIAL_SELL, PARTIAL_COVER(50%)
            # → PARTIAL_COVER.
            base_action = action.split("(", 1)[0].strip()
            if base_action not in sell_actions and base_action not in ("PARTIAL_SELL", "PARTIAL_COVER"):
                continue
            # A terminal-fail status that nevertheless moved shares IS a trim.
            # Filtering on fill_status alone (2026-07-16 audit) let a
            # partially-filled-then-canceled REDUCE fall through: the shares
            # left the book at midday, but close saw a clean slate and was free
            # to trim the same name again on the same soft flag — the exact
            # 2026-05-04 AMZN 41→21→11 double-trim this guard exists to stop.
            # `_trade_executed_or_pending` is the codebase's existing contract
            # for this (NULL/submitted/filled → yes; canceled/rejected/expired
            # → only when fill_qty > 0), and matches db._executed_trade_predicate.
            if not self._trade_executed_or_pending(r):
                continue
            sym = r.get("symbol")
            if sym:
                out.add(sym)
        return out

    def _file_target_revision(
        self,
        *,
        run_id: str,
        symbol: str,
        seat: str,
        evidence: str,
        code: str,
        applied: bool,
        trigger: str = "",
        prior_price: float | None = None,
        new_price: float | None = None,
        basis: str = "",
        level_used: float | None = None,
        detail: str = "",
        prior_code: str | None = None,
        bar_date: str | None = None,
    ) -> dict:
        """Write one adjudicated flag and return its payload.

        Persistence failure degrades to the in-memory payload (which still
        reaches the session result and the cockpit) rather than losing the
        outcome or raising — but it is logged as an error, because an
        unrecorded refusal is the blank this whole path exists to avoid.
        """
        payload = {
            "symbol": symbol,
            "code": code,
            "trigger": trigger,
            "seat": seat,
            "evidence": evidence,
            "detail": detail,
            "basis": basis,
            "prior_price": prior_price,
            "new_price": new_price,
            "level_used": level_used,
            "applied": bool(applied),
            # The daily close this read came from, so a message names that close
            # by its date instead of calling it "today's" before today closes.
            "bar_date": bar_date,
        }
        # FAULT 6 (item 194): an unapplied outcome identical to this
        # symbol's last one is recomputable state, and the sweep would
        # otherwise re-file it for every held name every session forever.
        # Same rule the trail's `record_trail_state_if_changed` applies:
        # the row marks a CHANGE. An applied revision is always written.
        if not applied and prior_code is not None and prior_code == code:
            payload["evidence_id"] = None
            payload["unchanged_since_last_session"] = True
            return payload

        evidence_id = None
        try:
            evidence_id = self.db.record_target_revision(
                run_id=run_id,
                symbol=symbol,
                code=code,
                seat=seat,
                evidence=evidence,
                detail=detail,
                trigger=trigger,
                prior_price=prior_price,
                new_price=new_price,
                basis=basis,
                level_used=level_used,
                applied=applied,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "target revision: failed to record %s outcome %s (%s)",
                symbol,
                code,
                exc,
            )
        payload["evidence_id"] = evidence_id
        if not applied:
            logger.info(
                "Target revision refused for %s: %s — %s",
                symbol,
                code,
                detail,
            )
        return payload

    def _trail_tightened_recently(self, symbol: str, calendar_days: int = 4) -> bool:
        """True when a non-canceled TRAIL_STOP for `symbol` landed within the
        last `calendar_days` days (a 4-calendar-day window is ~2-4 trading
        sessions depending on weekday: ~2 late in the week, ~4 from a
        Monday).

        RC1 forensics (2026-07-16): the reviewer's ≥1.02×old_stop min-bump
        rule means every ACCEPTED trail tightens ≥2%; per-session trailing
        marched stops into the daily-noise band in 3-4 sessions (GE was
        ratcheted 325→350 in 8 sessions on one flag). A cooldown makes
        tightening a considered, at-most-every-other-day act.
        """
        try:
            rows = self.db.get_trades(symbol=symbol, limit=10)
        except Exception as e:  # noqa: BLE001
            record_guarded_pass(self.db, "exit_records.trail_tightened_recently", e)
            logger.warning("trail cooldown query failed for %s: %s", symbol, e)
            return False
        from datetime import datetime as _dt, timedelta, timezone

        from src.trading_calendar import utc_now

        cutoff = utc_now() - timedelta(days=calendar_days)
        for row in rows:
            if (row.get("action") or "").upper() != "TRAIL_STOP":
                continue
            # NOTE (audit round 2): no fill_status filter here. A TRAIL_STOP
            # row is only written AFTER the broker accepted the replace, so
            # fill_status='canceled' means accepted-then-superseded (a later
            # trail replaced this stop) — the tighten still happened and is
            # still cooldown evidence. Skipping canceled rows silently
            # disabled the cooldown for exactly the ratchet chains it exists
            # to stop.
            # Ex-div adjustments also write TRAIL_STOP rows, but they LOWER
            # the stop (dividend-drop compensation) — counting them as a
            # "tighten" would hand every dividend payer a spurious cooldown.
            # Same idiom as the ex-div idempotence check.
            if "ex-div" in (row.get("reasoning") or "").lower():
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
            if dt >= cutoff:
                return True
        return False

    def _exit_event_risk_block(self, symbols: list[str]) -> str:
        """The fetched Event Risk section for an EXIT review.

        `RiskVerdict.reasoning_chain.event_risk` is a mandatory output field.
        The morning path fetches its answer (`RiskStage._build_event_risk_block`);
        this path passed nothing at all, so the renderer's NOT FETCHED fallback
        fired on all three sub-blocks and a mandatory question had no input.

        Earnings proximity IS fetchable here — `self.market` exists on the
        midday/close loop and the sweep is bounded per-symbol and in aggregate
        by the same `config.event_risk` timeouts the morning path uses. The
        macro-release and FOMC calendars are NOT: they are fetched by the
        morning research stage and no equivalent runs on this loop, so they
        render as the labelled NOT FETCHED form, which is the honest answer.

        Never raises. Any failure degrades to the fully-NOT-FETCHED block —
        an absent section reads as a calm calendar, which is the failure the
        block exists to prevent.
        """
        from src.data.event_calendar import (
            fetch_earnings_proximity,
            format_event_risk_block,
        )

        event_cfg = getattr(getattr(self, "config", None), "event_risk", None)
        horizon_days = getattr(event_cfg, "horizon_days", 10)
        earnings = None
        try:
            if symbols and getattr(self, "market", None) is not None:
                earnings = fetch_earnings_proximity(
                    self.market,
                    symbols,
                    per_symbol_timeout_s=getattr(
                        event_cfg,
                        "earnings_symbol_timeout_s",
                        8.0,
                    ),
                    total_deadline_s=getattr(event_cfg, "earnings_deadline_s", 20.0),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("Exit review: earnings proximity sweep failed: %s", e)
            earnings = None
        try:
            return format_event_risk_block(
                earnings=earnings,
                events=None,
                coverage=None,
                horizon_days=horizon_days,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Exit review: event-risk block render failed: %s", e)
            return format_event_risk_block(
                earnings=None,
                events=None,
                coverage=None,
                horizon_days=0,
            )

    def _record_exit_review_approvals(
        self,
        decisions,
        vetoed: set,
        verdict,
        *,
        run_id: str,
        original_action_by_symbol: dict,
    ) -> None:
        """One durable per-symbol row for every exit the AI Risk seat
        APPROVED on the exit-review path. Never raises.

        Board item 164 (2026-09-19). A veto here was already durable
        (`intraday_evaluations` plus an `exit_refusal` row), but an approval
        reached `agent_logs` only — one raw model response per run, with no
        per-symbol row saying "this exit was reviewed and let through, and
        why". Written to the exit path's own per-symbol record
        (`src/risk/exit_refusal.py`), which already carries non-drop
        outcomes (`dropped=False`, the fail-open codes) — NOT to the
        `pipeline_event` stream, because `src/refusal_signature.py` counts
        any surviving `pipeline_event` as the session having taken an idea,
        and an exit is not one. `ExitRiskVerdict` has no per-symbol approval
        reason, so the detail is the seat's own run-level reasoning, marked
        as such. Recording only: the returned veto set is unchanged.
        """
        from src.risk.exit_refusal import CODE_AI_RISK_APPROVED

        category = getattr(verdict, "reason_category", None)
        for d in decisions:
            if d.symbol in vetoed:
                continue
            self._record_exit_refusal(
                symbol=d.symbol,
                run_id=run_id,
                action=original_action_by_symbol.get(d.symbol, d.action),
                code=CODE_AI_RISK_APPROVED,
                dropped=False,
                detail=(
                    f"approved by the risk seat (category {category!r}; no "
                    f"per-symbol reason in the verdict, run-level reasoning "
                    f"follows): {verdict.reasoning or ''}"
                ),
                layer="ai_risk",
            )
