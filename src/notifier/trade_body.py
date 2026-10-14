"""Trade-session message body.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

from src.notifier.base import (
    _clip_text,
)
from src.notifier.sections import (
    _new_block,
    _new_section,
)
from src.notifier.markup import (
    _order_side,
    _order_summary,
)
from src.notifier.gaps import (
    _append_coverage_gap_banner,
)
from src.notifier.parts import (
    _append_company_identities,
    _append_leverage_line,
    describe_target_revisions,
)


def _append_trade_session_body(lines: list[str], result: dict) -> None:
    # audit round 2: "analysis_error" from a trading session means the PM
    # decision was never produced (LLM output unparseable / analysis step
    # failed) — its zero orders are a FAILURE artifact, not a deliberate
    # hold. Before this line the push looked identical to a quiet no-trade
    # day, so the operator could not tell "PM chose to sit out" from "PM
    # never spoke". Rendered first: it reframes everything below it.
    status = str(result.get("status", ""))
    failure_block: list[str] = []
    if status == "paid_analysis_suspended":
        failure_block.append(
            "🛑 SUSPENDED: paid LLM analysis is halted by the mandatory cost "
            "circuit. Broker protection and deterministic safety work "
            "remain active."
        )
        err = result.get("error")
        if err:
            # 900, not 300 — this is the deterministic cost-circuit
            # breaker's trigger detail, often a multi-clause sentence
            # (which ceiling, current spend, provider) worth reading in full.
            failure_block.append(f"trigger: {_clip_text(str(err), 900)}")
    elif status.startswith("pm_") or status == "analysis_error":
        failure_block.append(
            f"🛑 FAILED: PM decision failed ({status}) — no decisions were "
            "made; this is NOT a deliberate hold and the full paid stack "
            "will not auto-repeat"
        )
        err = result.get("error")
        if err:
            failure_block.append(f"error: {_clip_text(str(err), 900)}")
    _new_section(lines, *failure_block)

    # System-health first: a naked long is more urgent than the order list.
    _new_block(lines, _append_coverage_gap_banner, result)
    _new_block(lines, _append_leverage_line, result)
    orders = result.get("orders") or []

    def _render_orders(lines: list[str]) -> None:
        # FORCE_DELEVER / EMERGENCY_SELL / EMERGENCY_COVER banner — these
        # actions mean the autonomous loop intervened automatically.
        # force_delever fires when cash < -$1 (margin disabled) and
        # biggest-loser-first sells until cash >= 0. emergency_sell fires
        # from intra_check's / midday's flash-crash protection closing a
        # long; emergency_cover is the same circuit breaker covering a
        # SHORT (a distinct action name — not "emergency_sell" — because
        # it's a BUY, and reusing the SELL name here would also have to be
        # reused in db.py's realized-P&L FIFO lot matching, which assumes a
        # "sell-family" action closes a long against open BUY lots; a short
        # has no BUY lot to match against). All three look identical to a
        # routine order on the wire otherwise — operator's most important
        # "system intervened" signal would be invisible without this
        # banner. Kept glued to the order list right below it (same
        # section) rather than gapped off, since it's an annotation of
        # exactly those orders, not a separate topic.
        forced = [
            o
            for o in orders
            if isinstance(o, dict)
            and str(o.get("action", "")).upper()
            in (
                "FORCE_DELEVER",
                "EMERGENCY_SELL",
                "EMERGENCY_COVER",
            )
        ]
        if forced:
            actions = sorted({str(o.get("action", "")).upper() for o in forced})
            symbols = sorted({str(o.get("symbol", "?")) for o in forced})
            lines.append(
                f"🚨 AUTONOMOUS INTERVENTION ({', '.join(actions)}): {len(forced)} order(s) on {', '.join(symbols)}"
            )

        if orders:
            buys = [o for o in orders if _order_side(o) == "buy"]
            sells = [o for o in orders if _order_side(o) == "sell"]
            lines.append(f"orders: {len(orders)}  (BUY {len(buys)} / SELL {len(sells)})")
            # Show every order on its own line — operator wants to know what
            # was actually traded, not just a count. SELLs first (closing
            # context), then BUYs (opening context). 10-per-side cap is a
            # safety against unusual sessions; 99% of days are <10 each
            # and the full list fits in one Telegram message (4096 char limit).
            for o in sells[:10]:
                # Tag forced sells inline so operator can spot the specific
                # symbol that triggered the intervention banner above.
                action = str(o.get("action", "")).upper() if isinstance(o, dict) else ""
                label = "  SELL  "
                if action == "FORCE_DELEVER":
                    label = "  🚨FORCE"
                elif action == "EMERGENCY_SELL":
                    label = "  🚨EMER "
                lines.append(f"{label}{_order_summary(o)}")
            for o in buys[:10]:
                # EMERGENCY_COVER is a forced BUY (covering a short) — tag it
                # the same way the sells loop above tags a forced SELL, so the
                # operator can spot it without cross-referencing the banner.
                action = str(o.get("action", "")).upper() if isinstance(o, dict) else ""
                label = "  BUY   "
                if action == "EMERGENCY_COVER":
                    label = "  🚨EMER "
                lines.append(f"{label}{_order_summary(o)}")
            omitted = max(0, len(buys) - 10) + max(0, len(sells) - 10)
            if omitted:
                lines.append(f"  (+{omitted} more — see audit log)")
            _append_company_identities(
                lines,
                [o.get("symbol") for o in orders if isinstance(o, dict)],
            )
        else:
            lines.append("orders: 0")

    _new_block(lines, _render_orders)

    def _render_target_revisions(lines: list[str]) -> None:
        # The book this message carries, if any (a stored report's snapshot);
        # without one the renderer says so instead of naming unheld stocks.
        book = result.get("_positions")
        held = [p.get("symbol") for p in book if isinstance(p, dict)] if isinstance(book, list) else None
        lines.extend(describe_target_revisions(result, held_symbols=held))

    _new_block(lines, _render_target_revisions)

    from src import evidence_gate

    data_status = result.get("data_status") or {}
    degraded = [k for k, v in data_status.items() if evidence_gate.counts_as_degraded(v)]
    if degraded:
        _new_section(lines, f"⚠️ degraded: {', '.join(sorted(degraded))}")
