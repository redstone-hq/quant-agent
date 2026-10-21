"""src.prompt_facts.review.exits -- the loss-pit digest.

Bodies moved verbatim from src/pipeline_prompt_facts_review.py (`PromptFactsReviewMixin`),
which keeps same-named thin shims built per call. Every collaborator is an explicit
keyword-only constructor argument, so this builds and runs with no pipeline behind it.
"""

import logging

from src.storage.analytics.calibration import CLEAN_RECORD_DATE, CLEAN_RECORD_START_UTC

logger = logging.getLogger(__name__)


class ReviewExits:
    """The loss-pit digest; standalone, built from explicit collaborators."""

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

    def _build_recent_loss_pits(self) -> str:
        """PM L3f memory: repeat failure modes from losing BUYs.

        Reads `insights.buy_grades_json` for rows dated on/after the
        clean-record start (earlier rows are not evidence), pulls entries
        with `grade="wrong"` and a non-null `loss_root_cause`, groups by
        cause, keeps causes occurring ≥ 2 times. Output is prose PM renders
        directly — lets it see "greed_top_chasing × 3 over 14 days"
        BEFORE deciding today's sizing, not after another wrong entry.

        Empty string when no repeat pattern — PM's L3f section then shows
        a default "no recurring pits" note.
        """
        import json as _json

        try:
            rows = self.db.get_recent_insights(limit=None, since_date=CLEAN_RECORD_DATE)
        except Exception as e:
            logger.warning("recent_loss_pits: insights fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        cause_symbols: dict[str, list[str]] = {}
        cause_move: dict[str, list[float]] = {}
        cause_refs: dict[str, list[str]] = {}
        cause_dates: dict[str, list[str]] = {}
        for row in rows:
            row_date = (row.get("date") or "")[:10]
            if row_date < CLEAN_RECORD_DATE:
                continue
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
                    cause_dates.setdefault(cause, []).append(row_date)
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
            dates = cause_dates.get(cause, [])
            for i, s in enumerate(syms[:4]):
                m = moves[i] if i < len(moves) else None
                d = dates[i] if i < len(dates) else ""
                bits = ", ".join(b for b in (d, f"{m:+.1f}%" if m is not None else "") if b)
                detail_bits.append(f"{s} ({bits})" if bits else s)
            line = f"- {cause} × {n}: {', '.join(detail_bits)}"
            refs = cause_refs.get(cause, [])
            if refs and cause == "macro_warning_ignored":
                line += f' — ignored: "{refs[0]}"'
            lines.append(line)
        return "\n".join(lines)
