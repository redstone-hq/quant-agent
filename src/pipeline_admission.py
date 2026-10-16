"""Universe admission (step 8): `AdmissionService`, built from explicit collaborators.

Never sees a `TradingPipeline`; build it with `src.admission_build.build_admission_service`.
Patch `_get_sector` HERE, not on `src.pipeline`. Must not import `src.pipeline`.
"""

import logging
from collections.abc import Callable
from datetime import date

from src.sector_reference import _get_sector
from src.models import TechAnalysisResult, TradeDecision
from src.ports.event_journal import EventJournal

#: Logged under `src.pipeline`, as before the move (log records stay identical).
logger = logging.getLogger("src.pipeline")


class AdmissionService:
    """Universe admission, standalone: no `TradingPipeline` required."""

    def __init__(
        self,
        *,
        config,
        broker,
        market,
        journal: EventJournal,
        sec_form4_provider=None,
        constructor_cfg_fn: Callable[[], object | None] = lambda: None,
    ) -> None:
        self.config = config
        self.broker = broker
        self.market = market
        self.journal = journal
        self.sec_form4_provider = sec_form4_provider
        self._constructor_cfg_fn = constructor_cfg_fn

    def _constructor_cfg_or_none(self):
        return self._constructor_cfg_fn()

    def _filter_supported_symbols(
        self,
        decisions: list[TradeDecision],
        analyses: list[TechAnalysisResult],
        positions,
        admitted_symbols: set[str] | None = None,
    ) -> tuple[list[TradeDecision], list[str]]:
        universe = {symbol.strip().upper() for symbol in self.config.trading.universe}
        buy_allowlist = universe | {
            str(symbol).strip().upper() for symbol in (admitted_symbols or set()) if str(symbol).strip()
        }
        analyzed_symbols = {analysis.symbol.strip().upper() for analysis in analyses}
        held_symbols = {position.symbol.strip().upper() for position in positions}

        allowed_decisions: list[TradeDecision] = []
        blocked_reasons: list[str] = []

        for decision in decisions:
            symbol = decision.symbol.strip().upper()

            if decision.action == "BUY":
                if symbol not in buy_allowlist:
                    blocked_reasons.append(
                        f"{symbol} is neither in the configured universe nor "
                        "deterministically admitted for this run and cannot be bought"
                    )
                    continue
                if symbol not in analyzed_symbols:
                    blocked_reasons.append(
                        f"{symbol} has no supporting analyst output in this run and cannot be bought"
                    )
                    continue
            elif decision.action == "SELL" and symbol not in held_symbols:
                blocked_reasons.append(f"{symbol} is not an existing holding and cannot be sold")
                continue
            # Stage 3 (shorts). SHORT is the sell-side entry twin of BUY —
            # same universe/analyst-coverage bar, because it opens/adds new
            # risk the same way a BUY does. Without this explicit branch a
            # SHORT fell through to `allowed_decisions.append` unconditionally
            # (fail OPEN — the one thing D2 forbids), since it matched
            # neither the BUY nor the SELL condition above.
            elif decision.action == "SHORT":
                if symbol not in buy_allowlist:
                    blocked_reasons.append(
                        f"{symbol} is neither in the configured universe nor "
                        "deterministically admitted for this run and cannot be shorted"
                    )
                    continue
                if symbol not in analyzed_symbols:
                    blocked_reasons.append(
                        f"{symbol} has no supporting analyst output in this run and cannot be shorted"
                    )
                    continue
            # COVER is the buy-side exit twin of SELL — same held-position
            # bar. Same fail-OPEN gap as SHORT above without this branch.
            elif decision.action == "COVER" and symbol not in held_symbols:
                blocked_reasons.append(f"{symbol} is not an existing holding and cannot be covered")
                continue

            allowed_decisions.append(decision)

        return allowed_decisions, blocked_reasons

    def _evaluate_external_admission_gates(
        self,
        symbol: str,
        *,
        context: str = "external",
    ) -> tuple[bool, str | None, dict]:
        """Deterministic broker + market-quality gates for admitting a
        symbol OUTSIDE the configured universe.

        Shared by two callers that each decide WHICH symbols are worth
        gating (a different question) but must apply IDENTICAL gates once
        a symbol is a candidate: the SEC Form 4 smart-money transient-
        admission lane (`_admit_transient_smart_money_symbols`) and the
        Phase 9 nomination responder lane
        (`_admit_nominated_external_symbols`). The source of the candidate
        differs — a material Form 4 purchase vs. a research seat's
        nomination — but the trading-surface facts a candidate must clear
        before it can be bought (broker eligibility, price, liquidity,
        history, resolved sector) are exactly the same facts, so both
        callers share this one gate rather than each maintaining its own
        copy that could quietly drift apart.

        Returns ``(eligible, rejection_reason, details)``. ``details`` is
        populated only when eligible: ``last_price``,
        ``avg_dollar_volume_20d_usd``, ``sector``, ``broker``.
        ``rejection_reason`` is ``universe_screen_disabled`` when the
        universe screen is off (fail closed: the screen is the only
        admission check), else whatever the screen reports.
        """
        if not self._universe_screen_enabled():
            # Fail closed: the universe screen is the only admission check
            # (it replaced the old $10M dollar-volume / 20-day floors).
            logger.info("%s admission rejected %s: universe_screen_disabled", context, symbol)
            return False, "universe_screen_disabled", {}
        return self._evaluate_screened_admission(symbol, context=context)

    # ------------------------------------------------------------------
    # Universe expansion and pruning (src/universe_screen.py). Everything
    # below is inert while `universe_screen.enabled` is off.
    # ------------------------------------------------------------------

    def _universe_screen_enabled(self) -> bool:
        cfg = getattr(getattr(self, "config", None), "universe_screen", None)
        return bool(getattr(cfg, "enabled", False))

    def _universe_screen_sources(self, deadline: float, listed: dict | None = None):
        """The screen's read path: broker asset directory, yfinance bars and
        Nasdaq listing for size, sector and fund type, SEC filing history. Every source is read-only."""
        from src.data.sec_filings import screen_readers
        from src.sector_reference import _canonicalize_sector
        from src.universe_screen import HISTORY_FETCH_DAYS, ScreenSources

        # Company value and the fund flag come from the same Nasdaq listing the
        # daily record reads, never Yahoo: one source for every admission path.
        # An unavailable listing leaves every profile unreadable (undecided).
        from src.data.fund_category import FundCategoryStore
        from src.data.nasdaq_fetch import fetch_json
        from src.data.nasdaq_listing import ListingUnavailable, load_listing
        from src.universe_daily import _profile_reader, fund_category_reader

        try:
            listing = load_listing(fetch=fetch_json)
        except ListingUnavailable as exc:
            logger.warning("universe screen: Nasdaq listing unavailable: %s", exc)
            listing = None
        # A fund's category is what is kept in the desk database
        # (src/data/fund_category.py); a fund with none kept gets one Yahoo
        # read at the screen's profile stage, kept for next time, so this path
        # decides funds too (2026-10-14). A failed read leaves it undecided.
        store = FundCategoryStore(getattr(getattr(self.config, "storage", None), "db_path", None))
        _profile = _profile_reader(listing, _canonicalize_sector, store)

        provider = getattr(self, "sec_form4_provider", None)
        if provider is None:

            def _filings(symbol: str):
                raise RuntimeError("SEC provider not configured")

            get_role = None
        else:
            # Takeover rows with a locator plus the role reader (bidder vs
            # target from the filing header): `src/data/sec_filings.py`.
            _filings, get_role = screen_readers(provider, deadline, listed)

        return ScreenSources(
            get_asset=self.broker.get_asset_record,
            get_bars=lambda symbol: self.market.get_ohlcv(symbol, HISTORY_FETCH_DAYS),
            get_profile=_profile,
            get_filings=_filings,
            get_role=get_role,
            get_fund_category=fund_category_reader(store),
        )

    def _evaluate_screened_admission(
        self,
        symbol: str,
        *,
        context: str,
    ) -> tuple[bool, str | None, dict]:
        """The side-door gate when the universe screen is on: the SAME
        `screen_symbol` the weekly screen runs, so a Form 4 purchase or a
        seat's nomination can never admit a name the screen would refuse.
        Same return shape as the legacy gate it replaces."""
        import time as _time

        from src.universe_screen import ScreenThresholds, screen_symbol
        from src.util.time import et_today

        # Only the SEC reads take a deadline (the broker and yfinance reads
        # carry their own timeouts): at most the ticker-map refresh plus the
        # issuer filing history, one request timeout each.
        deadline = _time.monotonic() + float(self.config.smart_money.request_timeout_s) * 2
        result = screen_symbol(
            symbol,
            self._universe_screen_sources(deadline),
            ScreenThresholds.from_config(
                self.config,
                self._constructor_cfg_or_none(),
            ),
            today=et_today(),
        )
        if not result.passed:
            logger.info(
                "UNIVERSE_SCREEN %s admission rejected %s: %s",
                context,
                symbol,
                ", ".join(result.failures),
            )
            return False, result.reason, {}
        measured = dict(result.measured)
        return (
            True,
            None,
            {
                "last_price": measured.get("last_price"),
                "sector": measured.get("sector"),
                "screen": "universe_screen",
                "screen_measured": measured,
            },
        )

    def _form4_admission_is_current(self, observation) -> bool:
        """The Form 4 door's age gate, restored (screen on only).

        `lookback_days` went 7 -> 365 on 2026-09-11 and the provider's
        "stale" label is "older than lookback_days", so since then nothing
        inside the cache is ever stale and a 364-day-old purchase could
        admit a symbol (RSG came in that way). The bound is the desk's OWN
        horizon: a purchase disclosed more trading sessions ago than
        `risk.max_target_horizon_sessions` is older than the longest move
        the desk will claim a target for, so it cannot be the reason to
        open a new name now. Sessions are counted with the broker's
        holiday-aware calendar (item 165) — the plain weekday counter
        overstates the count by one per market holiday crossed, which
        skews this gate toward admitting names it should be rejecting.
        """
        from src.util.time import et_today

        disclosed = getattr(observation, "disclosure_date", None)
        if not isinstance(disclosed, date):
            return False
        horizon = int(self.config.risk.max_target_horizon_sessions)
        return self.broker.trading_sessions_held(disclosed, et_today()) <= horizon

    def _admit_screened_universe_symbols(self, positions=None) -> tuple[list[str], dict[str, dict]]:
        """Last night's screened passers for this morning, best score first (screen on only).

        Owner ruling 2026-10-14: send names in score order instead of rotating
        three at a time. Reads the nightly record (`src/universe_daily.py`) of
        the last completed session and returns ``(ranked, details)`` from
        `universe_screen.rank_passers`: every passer with a score K, highest
        first, and the details of every passer (unscored ones included, with
        the record's reason). The morning stage puts held, SEC-admitted and
        configured names ahead, sends the top `universe_screen.morning_top_n`
        and records every other passer with its reason. Raises when that
        session has no complete record, so the stage files a fault and runs
        with no screened name rather than with a stale list.
        """
        if not self._universe_screen_enabled():
            return [], {}
        from src.universe_daily import _screen_day, already_recorded, record_dir
        from src.trading_calendar import et_today, utc_now
        from src.universe_screen import rank_passers

        data_dir = self.config.universe_screen.data_dir
        day = _screen_day(self.broker, et_today(), utc_now())
        if day is None or not already_recorded(data_dir, day):
            raise FileNotFoundError(f"no complete nightly universe record for session {day}")
        return rank_passers(record_dir(data_dir) / f"{day.isoformat()}.jsonl")

    def _run_universe_screen(self, run_id: str) -> dict | None:
        """The weekly screen's incremental pass, run after the evening report
        (screen on only). Never raises: a failure costs tonight's pass,
        never the evening push. Every change is logged under
        `UNIVERSE_CHANGE`, kept in the state file until the morning message
        shows it, and written to the evidence record now."""
        if not self._universe_screen_enabled():
            return None
        import json as _json
        import time as _time

        from src.universe_screen import (
            HISTORY_FETCH_DAYS,
            ScreenThresholds,
            UniverseStore,
            run_screen,
        )
        from src.util.time import et_today

        cfg = self.config.universe_screen
        deadline = _time.monotonic() + float(cfg.screen_deadline_s)
        store = UniverseStore(cfg.data_dir)
        try:
            state = store.load()
            assets = self.broker.list_assets()
            held = {p.symbol.strip().upper() for p in self.broker.get_positions()}
            listed = None
            provider = getattr(self, "sec_form4_provider", None)
            if provider is not None:
                try:
                    listed = provider.listed_map(deadline)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("universe screen: SEC ticker map unavailable: %s", exc)
            run = run_screen(
                state,
                assets=assets,
                sources=self._universe_screen_sources(deadline, listed=listed),
                get_bars_batch=lambda chunk: self.market.get_ohlcv_batch(
                    chunk,
                    HISTORY_FETCH_DAYS,
                ),
                th=ScreenThresholds.from_config(
                    self.config,
                    self._constructor_cfg_or_none(),
                ),
                today=et_today(),
                held=held,
                configured=self.config.trading.universe,
                deadline=deadline,
                batch_size=int(cfg.bars_batch_size),
                confirm_missing_asset=self.broker.get_asset_record,
            )
            store.save(state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("UNIVERSE_SCREEN pass failed (non-fatal): %s", exc)
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        for event in run.events:
            self.journal.persist_evidence(
                run_id=run_id,
                agent_name="universe_screen",
                kind="universe_change",
                scope="symbol",
                symbol=event.get("symbol"),
                evidence_json=_json.dumps(event, sort_keys=True),
            )
        summary = run.summary()
        self.journal.persist_evidence(
            run_id=run_id,
            agent_name="universe_screen",
            kind="universe_screen_run",
            scope="run",
            evidence_json=_json.dumps(
                {k: v for k, v in summary.items() if k != "events"},
                sort_keys=True,
            ),
        )
        logger.info(
            "UNIVERSE_SCREEN pass: %d candidates, %d screened, %d passed, %d unreadable, %d change(s), deadline %s",
            run.candidates,
            run.screened,
            run.passed,
            run.inconclusive,
            len(run.events),
            "hit" if run.deadline_hit else "not hit",
        )
        return summary

    def _attach_universe_changes(self, result) -> None:
        """Hand the screen's unreported changes to the morning message, then
        mark them shown. Screen on only; fail-soft."""
        if not isinstance(result, dict) or not self._universe_screen_enabled():
            return
        from src.universe_screen import UniverseStore

        try:
            store = UniverseStore(self.config.universe_screen.data_dir)
            state = store.load()
            events = list(state.get("events") or [])
            result["universe_changes"] = {
                "events": events,
                "admitted_count": len(state.get("admitted") or {}),
                "flagged_count": sum(1 for r in (state.get("admitted") or {}).values() if r.get("status") == "flagged"),
            }
            if events:
                state["events"] = []
                store.save(state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("universe changes could not be attached: %s", exc)

    def _admit_nominated_external_symbols(
        self,
        symbols: list,
    ) -> tuple[set[str], dict[str, dict]]:
        """Phase 9 (§9.1/§9.2) — admit nominated symbols OUTSIDE the
        configured universe.

        No LLM output (a nomination) can grant BUY eligibility on its
        own — only the deterministic gates in
        `_evaluate_external_admission_gates` can, the SAME gates the
        SEC Form 4 smart-money lane already applies. A symbol already
        inside the configured universe never reaches this function; the
        caller (`MorningResearchStage._run_nomination_responder_pass`)
        filters those out first since they need no gate at all.

        Unlike `_admit_transient_smart_money_symbols`, there is no cap
        applied HERE — the caller has already applied the per-seat and
        global nomination caps (`src.nominations.select_nominations`)
        before a symbol ever reaches this gate, so every symbol passed in
        is already a bounded, ranked candidate.
        """
        admitted: set[str] = set()
        details: dict[str, dict] = {}
        for symbol in sorted({str(s).strip().upper() for s in symbols if str(s).strip()}):
            eligible, _reason, gate_details = self._evaluate_external_admission_gates(
                symbol,
                context="nomination",
            )
            if not eligible:
                continue
            details[symbol] = {
                "temporary": True,
                "reason": "nomination_external_admission",
                **gate_details,
            }
            admitted.add(symbol)
        return admitted, details

    def _admit_transient_smart_money_symbols(
        self,
        observations: list,
    ) -> tuple[set[str], dict[str, dict]]:
        """Apply broker and market-quality gates to SEC-qualified purchases.

        The source provider owns filing provenance, P/S parsing, recency,
        materiality and independent-owner clustering. This second gate owns
        the trading-surface facts the SEC cannot know: Alpaca eligibility,
        price, history and liquidity — via `_evaluate_external_admission_gates`,
        shared with the Phase 9 nomination responder lane
        (`_admit_nominated_external_symbols`). The output lives only on
        RunContext.
        """
        cfg = self.config.smart_money
        configured = {str(symbol).strip().upper() for symbol in self.config.trading.universe if str(symbol).strip()}
        screen_on = self._universe_screen_enabled()
        grouped: dict[str, list] = {}
        for observation in observations or []:
            symbol = str(getattr(observation, "symbol", "") or "").strip().upper()
            if not symbol or symbol in configured:
                continue
            if str(getattr(observation, "transaction_code", "") or "").upper() != "P":
                continue
            if not bool(getattr(observation, "admission_eligible", False)):
                continue
            if screen_on and not self._form4_admission_is_current(observation):
                logger.info(
                    "UNIVERSE_SCREEN SEC transient admission skipped %s: purchase "
                    "disclosed %s, older than the desk's %d-session horizon",
                    symbol,
                    getattr(observation, "disclosure_date", "?"),
                    int(self.config.risk.max_target_horizon_sessions),
                )
                continue
            grouped.setdefault(symbol, []).append(observation)

        def _rank(item):
            symbol, rows = item
            value = sum(float(getattr(row, "transaction_value_usd", 0) or 0) for row in rows)
            newest = max(str(getattr(row, "known_at", "") or "") for row in rows)
            return (-value, newest, symbol)

        admitted: set[str] = set()
        details: dict[str, dict] = {}
        for symbol, rows in sorted(grouped.items(), key=_rank):
            if len(admitted) >= cfg.max_external_candidates:
                break
            eligible, _reason, gate_details = self._evaluate_external_admission_gates(
                symbol,
                context="SEC transient",
            )
            if not eligible:
                continue
            accessions = sorted(
                {
                    str(getattr(row, "accession_number", "") or "")
                    for row in rows
                    if getattr(row, "accession_number", None)
                }
            )
            total_value = round(sum(float(getattr(row, "transaction_value_usd", 0) or 0) for row in rows), 2)
            owners = sorted(
                {
                    str(getattr(row, "actor", "") or "").strip()
                    for row in rows
                    if str(getattr(row, "actor", "") or "").strip()
                }
            )
            # Every admitting row is opportunistic by construction — the
            # provider strips routine purchases from ``admission_eligible``.
            # Carrying the reasons through anyway makes the operator's
            # admission record self-explaining rather than requiring a
            # re-derivation from the raw filing.
            signal_reasons = sorted(
                {
                    str(getattr(row, "signal_class_reason", "") or "")
                    for row in rows
                    if getattr(row, "signal_class_reason", "")
                }
            )
            details[symbol] = {
                "temporary": True,
                "reason": "material_sec_form4_purchase",
                "signal_class": "opportunistic",
                "signal_class_reasons": signal_reasons,
                "accessions": accessions,
                "owners": owners,
                "transaction_value_usd": total_value,
                **gate_details,
            }
            admitted.add(symbol)
        return admitted, details
