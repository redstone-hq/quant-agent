"""src.intraday.candidates -- the paid scan's wrapper and its inputs: the process-locked entry,
held-technical refresh, mover candidates, the ATR move context and the two skip records.

Bodies moved verbatim from src/pipeline_intraday.py (`IntradayMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Host
attributes a body both reads and ASSIGNS go through `state` (a get/set view the shim
hands in), never a construction-time copy.
"""

import logging
import math

from src.pipeline_context import RunContext
from src.sentinel.guarded_site import record_site as _site
from src.pipeline_stages import _record_pipeline_event

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


def record_dropped_name(owner, ctx, symbol: str, reason: str, **details) -> None:
    """One durable pipeline event per name the scan drops, once per run and reason.

    stage=opportunity, outcome=dropped, reason no_atr | budget_refused |
    not_sent_weaker_rank | pm_cadence. Never raises: the scan must not fail on its
    own bookkeeping. Deliberately NOT an intraday_evaluations row, because a
    row there means the PM saw the name and sets its next reference price.

    `move_pct` on every record is the RANKED move: |last - reference| as a
    percent of the reference, where the reference is the price at the desk's
    last look this session, else the prior close (see `_reference_price`).
    """
    seen = ctx.__dict__.setdefault("_dropped_seen", set())
    if (symbol, reason) in seen:
        return
    seen.add((symbol, reason))
    try:
        _record_pipeline_event(owner, ctx, symbol, "opportunity", "dropped", reason, **details)
    except Exception as exc:  # noqa: BLE001
        _site(owner, "dropped_name_event", exc, context={"symbol": symbol, "reason": reason}, log=logger)


class IntradayCandidates:
    """The paid scan's wrapper and its inputs: the process-locked entry, held-technical refresh, mover
    candidates, the ATR move context and the two skip records. Standalone, built from explicit
    collaborators.
    """

    def __init__(
        self,
        *,
        config=None,
        broker=None,
        db=None,
        sweeper=None,
        retired_cash_park_symbol=None,
        intraday_opportunity_scan_body=None,
        intraday_scan_process_lock=None,
        track_intraday_snapshot_ok=None,
        track_intraday_snapshot_miss=None,
        blocking_owner_session=None,
        atr_for_symbol=None,
    ) -> None:
        self.config = config
        self.broker = broker
        self.db = db
        self._sweeper = sweeper
        self._retired_cash_park_symbol = retired_cash_park_symbol
        self._intraday_opportunity_scan_body = intraday_opportunity_scan_body
        self._intraday_scan_process_lock = intraday_scan_process_lock
        self._track_intraday_snapshot_ok = track_intraday_snapshot_ok
        self._track_intraday_snapshot_miss = track_intraday_snapshot_miss
        self._blocking_owner_session = blocking_owner_session
        self._atr_for_symbol = atr_for_symbol

    def _run_intraday_opportunity_scan(self, ctx: RunContext) -> dict:
        """Concurrency-guarded wrapper around the scan body.

        2026-08-31 visibility fix: every path through this wrapper and the
        body it delegates to now returns an explicit result dict — never a
        bare None — so run_intra_check's `intraday_scan` key distinguishes
        the three everyday reasons a tick adds no new activity from EACH
        OTHER and from a crash. PR #163 (2026-08-30) made a crashed scan
        visible as "intraday_scan_crashed" but left these three still
        collapsed onto the identical absent-key shape:

          - "intraday_scan_disabled": the feature is off in config.
          - "intraday_scan_lock_contended": another scan already owns this
            window — either this process's own advisory flock (see
            `_intraday_scan_process_lock`) or a morning/midday/close
            wrapper that still holds the owner lock at the end of this
            tick's wait (`_await_paid_scan_slot`).
          - "intraday_scan_open_overlap": morning released the owner lock
            on this same 09:30-shared tick — still the open, not a real
            INTRADAY look (item 121; see `_intraday_open_overlap_skip`).
          - "intraday_scan_no_opportunity": the scan ran and found nothing
            worth escalating (see `_intraday_opportunity_scan_body`'s
            early-return points).

        All three are HEALTHY completions — see ops/rehearsal/report.py's
        STATUS_PLAIN entries and `_verdict`'s healthy set, which is where
        "intraday_scan_crashed" is deliberately NOT included.
        """
        cfg = getattr(self.config, "intraday_scan", None)
        if cfg is None or not getattr(cfg, "enabled", False):
            return {"status": "intraday_scan_disabled", "run_id": ctx.run_id}
        with self._intraday_scan_process_lock() as acquired:
            if not acquired:
                return {"status": "intraday_scan_lock_contended", "run_id": ctx.run_id}
            return self._intraday_opportunity_scan_body(ctx)

    def _intraday_held_tech_symbols(self, ctx: RunContext) -> list[str]:
        """Investable holdings that need current-run Technical on this scan.

        Morning's Tech pre-filter already includes every held name
        (`_has_actionable_signal_fn`). The intraday scan used to send only
        names that moved past the threshold, so a quiet hold the PM can
        still increase had no current-run Technical: grounding failed the
        whole paid decision (`pm_grounding_error`, "increase lacks a
        current-run Technical analysis"). Missing specialist data is a
        defect in the producing step — this list is that step. Cash-park
        vehicles have no thesis and stay out.
        """
        parked: set[str] = set()
        sweeper = self._sweeper()
        investable = list(ctx.positions or [])
        if sweeper is not None:
            investable, _parked = sweeper.split_positions(investable)
        retired = self._retired_cash_park_symbol()
        if isinstance(retired, str) and retired.strip():
            parked.add(retired.strip().upper())
        seen: set[str] = set()
        out: list[str] = []
        for pos in investable:
            if not getattr(pos, "qty", 0):
                continue
            symbol = str(getattr(pos, "symbol", "") or "").strip().upper()
            if not symbol or symbol in seen or symbol in parked:
                continue
            seen.add(symbol)
            out.append(symbol)
        return out

    def _reference_price(self, symbol: str, prev_close: float) -> tuple[float, str]:
        """The price this name's move is measured from, and which one it is.

        The price recorded at the desk's latest look at the name THIS session
        ("last_look"), else the prior close ("prev_close"). A name the desk
        has not looked at today has, as its last seen price, yesterday's
        close, so both are "the move since the desk last saw the name" and
        rank against each other in one list. A row with no stored price
        (pre-2026-10-13, or an exit-path row) and an unreadable ledger both
        fall back to the prior close, the never-looked-at rule; neither
        locks the name out.
        """
        reader = getattr(self.db, "get_session_reference_price", None)
        try:
            ref = reader(symbol) if callable(reader) else None
        except Exception as exc:  # noqa: BLE001 - the name still ranks, from the prior close
            _site(self, "reference_price_read", exc, context={"symbol": symbol}, log=logger)
            ref = None
        if isinstance(ref, (int, float)) and not isinstance(ref, bool) and math.isfinite(ref) and ref > 0:
            return float(ref), "last_look"
        return float(prev_close), "prev_close"

    def _intraday_scan_mover_candidates(
        self,
        ctx: RunContext,
    ) -> tuple[list[tuple[str, float]], dict]:
        """Cheap snapshot of who moved, ranked by move / own ATR. No paid calls.

        Returns (symbol, move_pct) pairs, largest move-in-ATR-multiples
        first. Every name with a readable ATR is returned; there is no cap,
        no move threshold and no re-look clock. The move is measured from
        the desk's last look at the name this session, else the prior close
        (`_reference_price`), so a name looked at an hour ago competes on
        what it did since, in the same ranking as every fresh mover. The
        ranked `move_pct` is the one every downstream record carries; the
        live price and its reference ride on `ctx._mover_rank_basis` for the
        evaluation row and its ATR stamp.

        Runs before the owner-lock wait so a contended morning/midday
        cannot vanish the mover list. A skip after wait names these
        symbols in a durable reason instead of dropping them silently.
        """
        from src.data.live_price import (
            NO_PRICE_AT_ALL,
            NO_SNAPSHOT,
            resolve_live_price,
        )

        universe = list(self.config.trading.universe)
        snapshots = self.broker.get_intraday_snapshots(universe) or {}
        if not snapshots:
            return [], {}
        ranked: list[tuple[str, float, float]] = []
        basis_by_symbol: dict[str, dict] = {}
        ctx.__dict__["_mover_rank_basis"] = basis_by_symbol
        for symbol in universe:
            snap = snapshots.get(symbol) or {}
            # item 120: the move that buys a PAID look has to be today's.
            # This read `last_price` straight, so a name still carrying a
            # prior session's print measured a move that did not happen
            # today. Same resolver as the morning Tech pass, so "today" is
            # decided in one place.
            resolved = resolve_live_price(snap)
            last = resolved.price
            prev = snap.get("prev_close")
            # The miss counter pages the owner after three consecutive
            # scans with "check whether the ticker is still valid/tradable
            # on Alpaca". It exists to tell a BROKEN ticker from a quiet
            # one (`src/storage/db.py`), so a thin name that simply has not
            # printed today must NOT feed it — item 120's own filing names
            # two IEX-thin names in exactly that state, and paging on them
            # would be a false alarm. A symbol the feed returned nothing
            # for at all is still a miss.
            fed_nothing = resolved.unavailable in (NO_SNAPSHOT, NO_PRICE_AT_ALL)
            if not isinstance(prev, (int, float)) or (last is None and fed_nothing):
                self._track_intraday_snapshot_miss(symbol)
                continue
            self._track_intraday_snapshot_ok(symbol)
            if last is None:
                # Quiet, not broken: the feed answered, the name has no
                # today print. It cannot have moved today, so it buys no
                # paid look — and it does not page anybody either.
                continue
            if prev <= 0:
                continue
            ref, basis = self._reference_price(symbol, prev)
            move_pct = abs(last - ref) / ref * 100.0
            basis_by_symbol[symbol.upper()] = {"last": float(last), "ref_price": ref, "move_ref": basis}
            # Rank by the move in the name's OWN range (ATR as a percent of
            # the reference), never a flat percentage: 2% on a 1%/day name
            # outranks 4% on an 8%/day name. Same reference on both sides,
            # so move_atr is exactly |last - ref| / ATR. A name whose ATR
            # cannot be read is dropped with a recorded reason, never given
            # a made-up one.
            atr_pct, move_atr = self._intraday_move_in_atr(move_pct, self._read_atr(symbol), ref)
            if atr_pct is None or move_atr is None:
                record_dropped_name(self, ctx, symbol, "no_atr", move_pct=move_pct, move_ref=basis)
                continue
            ranked.append((symbol, move_pct, move_atr))
        # Largest move-in-own-ATR first. No move threshold: the scan body
        # sends only the strongest-ranked batch (owner ruling 2026-10-12).
        ranked.sort(key=lambda t: -t[2])
        candidates = [(symbol, move_pct) for symbol, move_pct, _ in ranked]
        return candidates, snapshots

    def _read_atr(self, symbol: str) -> float | None:
        """The pipeline's own ATR(14) for `symbol`; None when unavailable or unreadable."""
        reader = self._atr_for_symbol
        if not callable(reader):
            return None
        try:
            return reader(symbol)
        except Exception as exc:  # noqa: BLE001 - recorded as no_atr by the caller
            _site(self, "intraday_rank_atr", exc, context={"symbol": symbol}, log=logger)
            return None

    @staticmethod
    def _intraday_move_in_atr(
        move_pct: float,
        atr_14: float | None,
        prev_close: float | None,
    ) -> tuple[float | None, float | None]:
        """The trigger's move expressed in the NAME'S OWN daily range.

        Board item 177, the trigger third. `move_threshold_pct` is a flat
        3% applied to every symbol alike, and the number ledger's open
        question against it asks what move size *relative to the name's own
        ATR* marks a development worth re-reading. That question cannot be
        answered from the desk's record, because the record never held the
        denominator: `intraday_evaluations.detail` stored `move_pct=` and
        nothing else, so 253 recorded selections (2026-09-02 -> 2026-09-25)
        say how far a name moved and never how far that name normally
        moves. Measured on those 253 rows, the flat threshold does not
        discriminate at all — the median move of a selection that produced
        a BUY/SHORT is 3.50% against 3.67% for one that produced nothing,
        and the 5-7% band produced zero orders from 51 selections — so
        re-picking the flat number in either direction has no basis, and
        the ATR-relative form has no data yet. This records the
        denominator, on bars the scan already paid to fetch. The flat
        threshold, the name cap and the 3h re-look cooldown have since
        all been deleted; this is now the scan's one rank key.

        `prev_close` is the price the move was measured from: the prior
        close, or the price at the desk's last look this session (see
        `_reference_price`). Returns (atr_pct_of_that_price,
        move_in_atr_multiples), either of which is None when the inputs
        cannot support it.
        """
        if not isinstance(atr_14, (int, float)) or atr_14 <= 0:
            return None, None
        if not isinstance(prev_close, (int, float)) or prev_close <= 0:
            return None, None
        atr_pct = float(atr_14) / float(prev_close) * 100.0
        if atr_pct <= 0:
            return None, None
        return atr_pct, float(move_pct) / atr_pct

    def _record_intraday_trigger_atr_context(
        self,
        ctx: RunContext,
        symbol: str,
        mover_symbols: set[str],
        move_by_symbol: dict,
        snapshots: dict,
        indicators,
    ) -> None:
        """Stamp the ATR denominator onto the detail of a mover's ledger row.

        Held on the run context and written with the row itself, which
        records the price the PM saw the mover at and is written only once
        the PM has seen it (adversary review 2026-10-12): no new row, no
        extra market or model call. Best-effort — a measurement must never
        cost the scan.

        `move_pct` is the ranked move (since the reference in
        `ctx._mover_rank_basis`), so the ATR here is taken against that same
        reference and `move_atr` matches the rank key; `move_ref` names it.
        """
        upper = symbol.upper()
        if upper not in mover_symbols:
            return
        move_pct = move_by_symbol.get(symbol, move_by_symbol.get(upper))
        if not isinstance(move_pct, (int, float)):
            return
        snap = snapshots.get(symbol) or snapshots.get(upper) or {}
        basis = (ctx.__dict__.get("_mover_rank_basis") or {}).get(upper) or {}
        atr_pct, move_atr = self._intraday_move_in_atr(
            float(move_pct),
            getattr(indicators, "atr_14", None),
            basis.get("ref_price", snap.get("prev_close")),
        )
        detail = f"move_pct={float(move_pct):.4f}"
        if atr_pct is None or move_atr is None:
            detail += ";atr_pct=unreadable;move_atr=unreadable"
        else:
            detail += f";atr_pct={atr_pct:.4f};move_atr={move_atr:.4f}"
        detail += f";move_ref={basis.get('move_ref', 'prev_close')}"
        ctx.__dict__.setdefault("_mover_ledger_detail", {})[upper] = detail

    def _intraday_paid_scan_skip(self, ctx: RunContext, movers: list[str]) -> dict:
        """Durable skip: lock still held, movers named, no silent drop."""
        blocking = self._blocking_owner_session() or "owner_lock"
        named = ",".join(movers) if movers else "none"
        reason = f"paid discovery skipped: {blocking} still held; movers={named}"
        logger.warning("Intraday scan: %s", reason)
        for symbol in movers:
            try:
                _record_pipeline_event(
                    self,
                    ctx,
                    symbol,
                    "opportunity",
                    "skipped",
                    "intraday_scan_lock_contended",
                    detail=reason,
                )
            except Exception as exc:  # noqa: BLE001
                _site(self, "skip_reason_lock_contended", exc, context={"symbol": symbol})
        return {
            "status": "intraday_scan_lock_contended",
            "run_id": ctx.run_id,
            "reason": reason,
            "movers": list(movers),
        }

    def _intraday_open_overlap_skip(self, ctx: RunContext, movers: list[str]) -> dict:
        """Morning released the lock on this same 09:30-shared tick.

        Not a lock contention (morning is no longer holding it) and not a
        real INTRADAY opportunity — running paid discovery here would be
        the measured 09:37 leftover (item 121): the SAME open, sold to the
        owner a second time under a different label. Skip; the next
        existing half-hour fire, which sees no lock at all, runs normally.
        """
        named = ",".join(movers) if movers else "none"
        reason = f"paid discovery skipped: this fire shares the 09:30 open with morning; movers={named}"
        logger.info("Intraday scan: %s", reason)
        for symbol in movers:
            try:
                _record_pipeline_event(
                    self,
                    ctx,
                    symbol,
                    "opportunity",
                    "skipped",
                    "intraday_scan_open_overlap",
                    detail=reason,
                )
            except Exception as exc:  # noqa: BLE001
                _site(self, "skip_reason_open_overlap", exc, context={"symbol": symbol})
        return {
            "status": "intraday_scan_open_overlap",
            "run_id": ctx.run_id,
            "reason": reason,
            "movers": list(movers),
        }
