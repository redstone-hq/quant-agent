"""Name-coverage record step (moved verbatim from TradingPipeline)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class NameCoverageRecordSession:
    """Name-coverage record step (moved verbatim from TradingPipeline)."""

    def __init__(
        self,
        *,
        config,
    ) -> None:
        self._config = config

    def run(self, ctx, _record) -> None:
        """Write down, per candidate name, which seats answered ABOUT it.

        The counting half of docs/WORK.md item 20. It is a RECORD, not a
        bar: no ratio, no minimum, nothing refused here. Both attempts at
        deriving a coverage bar failed and the reasons are written down in
        `src/evidence_name_coverage.py`; this is the
        instrument that would let one be measured from the desk's own data
        instead of picked. Fail-soft — a forensic record must never be able
        to break the trading path it reports on.
        """
        from src.evidence_gate import BLOCKING_SEATS
        from src.evidence_name_coverage import build_name_coverage_recorder

        # The recorder is built HERE, from values this call site owns: the
        # owner's blocking-seat mandate set comes from the gate, the seat
        # lists from the record module's own defaults. The record module
        # reaches back into nothing.
        recorder = build_name_coverage_recorder(blocking_seats=BLOCKING_SEATS)

        try:
            seat_symbols: dict[str, set] = {}
            seat_symbols["tech"] = {getattr(a, "symbol", "") for a in (getattr(ctx, "analyses", None) or ())}
            seat_symbols["earnings"] = {
                (r.get("symbol") if isinstance(r, dict) else getattr(r, "symbol", ""))
                for r in (getattr(ctx, "earnings_results", None) or ())
            }
            # The smart-money seat's own shortlist (2026-10-19): every name it
            # was shown, with a summary line or a recorded no-filings reason.
            # `smart_money_observations` is now every market-wide row, so it
            # would claim thousands of names the seat never read.
            smart: set = {str(s) for s in (getattr(ctx, "smart_money_shortlist", None) or ())}
            for item in getattr(ctx, "smart_money_findings", None) or ():
                smart.add(item.get("symbol") if isinstance(item, dict) else getattr(item, "symbol", ""))
            seat_symbols["smart_money"] = smart
            intel = getattr(ctx, "news_intel", None)
            if intel is not None:
                # Absent `news_intel` means the news seat recorded no
                # per-name coverage at all, which `name_coverage` reports as
                # uncovered rather than assuming complete.
                seat_symbols["news"] = set(getattr(intel, "stock_news", None) or {})

            universe: set = set()
            for names in seat_symbols.values():
                universe |= {n for n in names if n}
            universe |= {str(s) for s in (getattr(ctx, "admitted_symbols", None) or set())}
            # HELD NAMES ARE IN THE UNIVERSE (board item 220). The rule binds
            # on STAYING as well as entering, and a held name that no seat
            # answered about this review was previously absent from this
            # record entirely — the one case where "no row" meant "nothing to
            # see" rather than "nobody looked".
            for pos in getattr(ctx, "positions", None) or ():
                sym = pos.get("symbol") if isinstance(pos, dict) else getattr(pos, "symbol", "")
                if sym:
                    universe.add(str(sym))
            # A name whose technical row came back unreadable may be in no
            # other list at all, and it is the one name that must not vanish.
            unreadable_by_seat = {
                "tech": set(getattr(ctx, "tech_unreadable", None) or {}),
            }
            asked_no_answer_by_seat = {
                "tech": set(getattr(ctx, "tech_unanswered", None) or set()),
            }
            universe |= {str(s) for s in unreadable_by_seat["tech"]}
            universe |= {str(s) for s in asked_no_answer_by_seat["tech"]}
            try:
                universe |= {str(s) for s in self._config.trading.universe}
            except Exception:  # noqa: BLE001 — config shape is not this record's job
                pass

            coverage_by_name = recorder.coverage(
                universe,
                seat_symbols,
                unreadable_by_seat=unreadable_by_seat,
                asked_no_answer_by_seat=asked_no_answer_by_seat,
            )
            for name, coverage in coverage_by_name.items():
                record = coverage.to_evidence()
                # `symbol` is the call's own first argument and `_record`
                # fixes the stage itself; passing either again raised
                # "multiple values" TypeErrors (symbol, then stage).
                record.pop("symbol", None)
                _record(
                    name,
                    "recorded",
                    record.pop("summary"),
                    gate="name_coverage",
                    **record,
                )

            # The per-name reading of the owner's blocking-seat mandate,
            # carried out of here so the entry bar and the holding review
            # both read a MISSING seat rather than an absent objection.
            # Nothing is refused here; the categorical refusals already
            # exist (`risk.rules.own_bar_block_reason` for entry, rotation's
            # `ineligible_hold` tier for the held side) and both already
            # treat "no technical read this review" as blocking.
            gaps = recorder.names_missing_blocking_seat(coverage_by_name)
            ctx.name_coverage_blocking_gaps = dict(gaps)
            if gaps:
                logger.warning(
                    "evidence gate: %d name(s) have NO answer from a seat that "
                    "may stop the desk — treated as a missing seat, never as "
                    "agreement: %s%s",
                    len(gaps),
                    "; ".join(f"{n}={','.join(seats)}" for n, seats in sorted(gaps.items())),
                    (
                        " (returned-but-unreadable: " + ", ".join(sorted(unreadable_by_seat["tech"])) + ")"
                        if unreadable_by_seat["tech"]
                        else ""
                    ),
                )
        except Exception:  # noqa: BLE001 — never break the decision
            # Stays broad on purpose: this runs on the trading path and a
            # forensic record may not stop it. It is no longer QUIET: the
            # traceback is logged at ERROR so a programming fault (the
            # duplicate-symbol TypeError hid here as one warning line) is
            # unmistakable. Rows landing is proven by a store-level test.
            logger.exception("evidence gate: name coverage write failed")
