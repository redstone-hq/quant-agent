"""The trade-review seat as ONE standalone part: `PromptFactsReview`.

Built alone from its collaborators only -- `PromptFactsReview(db=..., broker=..., market=...,
sweeper=..., exit_audit_actions=...,
log_conviction_outcome_for_operator=...)` -- and exercised with nothing behind it. The part
knows NO host: it never reaches back into the pipeline for anything. The holder
(`src/pipeline_prompt_facts_review.py`) rebuilds one per call from the seat's current
collaborators, so a collaborator swapped on the seat is the one the next call sees without the
part holding a reference to the seat.

The five `_review_*` builders and the eleven same-named shims are moved VERBATIM (AST-identical)
from `PromptFactsReviewMixin` (src/pipeline_prompt_facts_review.py, retired 2026-10-04). The
prompt-facts seat HOLDS one of these, installed by `hold_prompt_facts_review`, and inherits
nothing from it; the fact bodies themselves stay on the five family parts in this package.
"""

from src.prompt_facts.review.blocked import ReviewBlocked
from src.prompt_facts.review.calibration import ReviewCalibration
from src.prompt_facts.review.exits import ReviewExits
from src.prompt_facts.review.grading import ReviewGrading
from src.prompt_facts.review.replay import ReviewReplay

#: The part's keyword collaborators, in constructor order. Every one defaults to None so the
#: part builds bare; the family parts tolerate a missing collaborator exactly as before.
COLLABORATORS = (
    "db",
    "broker",
    "market",
    "sweeper",
    "exit_audit_actions",
    "log_conviction_outcome_for_operator",
)


class PromptFactsReview:
    """Trade-review prompt facts; every body lives on a part under src/prompt_facts/review/.

    Each `_review_*` builder hands the part's collaborators to one family part."""

    def __init__(
        self,
        *,
        db=None,
        broker=None,
        market=None,
        sweeper=None,
        exit_audit_actions=None,
        log_conviction_outcome_for_operator=None,
    ) -> None:
        self.db = db
        self.broker = broker
        self.market = market
        self.sweeper = sweeper
        self.exit_audit_actions = exit_audit_actions
        self.log_conviction_outcome_for_operator = log_conviction_outcome_for_operator

    def _review_grading(self) -> ReviewGrading:
        return ReviewGrading(
            db=self.db,
            broker=self.broker,
            market=self.market,
            sweeper=self.sweeper,
        )

    def _review_exits(self) -> ReviewExits:
        return ReviewExits(
            db=self.db,
            broker=self.broker,
            sweeper=self.sweeper,
            exit_audit_actions=self.exit_audit_actions,
        )

    def _review_calibration(self) -> ReviewCalibration:
        return ReviewCalibration(
            db=self.db,
            log_conviction_outcome_for_operator=self.log_conviction_outcome_for_operator,
        )

    def _review_blocked(self) -> ReviewBlocked:
        return ReviewBlocked(db=self.db)

    def _review_replay(self) -> ReviewReplay:
        return ReviewReplay()

    def _build_recent_sells_for_grading(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/grading.py."""
        return self._review_grading()._build_recent_sells_for_grading(*args, **kwargs)

    def _build_recent_buys_for_grading(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/grading.py."""
        return self._review_grading()._build_recent_buys_for_grading(*args, **kwargs)

    def _build_trade_grade_summary(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/grading.py."""
        return self._review_grading()._build_trade_grade_summary(*args, **kwargs)

    def _build_recent_missed_lessons(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/exits.py."""
        return self._review_exits()._build_recent_missed_lessons(*args, **kwargs)

    def _build_recent_loss_pits(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/exits.py."""
        return self._review_exits()._build_recent_loss_pits(*args, **kwargs)

    def _build_recent_outlook_calibration(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/calibration.py."""
        return self._review_calibration()._build_recent_outlook_calibration(*args, **kwargs)

    def _build_calibration_note(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/calibration.py."""
        return self._review_calibration()._build_calibration_note(*args, **kwargs)

    def _compute_recent_performance(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/calibration.py."""
        return self._review_calibration()._compute_recent_performance(*args, **kwargs)

    def _build_blocked_proposals(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/blocked.py."""
        return self._review_blocked()._build_blocked_proposals(*args, **kwargs)

    def _persist_evening_replay_inputs(self, *args, **kwargs):
        """Thin shim: body moved to src/prompt_facts/review/replay.py."""
        return self._review_replay()._persist_evening_replay_inputs(*args, **kwargs)
