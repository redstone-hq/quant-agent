"""src.prompt_facts.review.exits -- the exits review boundary.

The loss-pit digest (`_build_recent_loss_pits`) that lived here was removed
2026-10-21 (owner ruling q23): it fed the evening AI's loss-cause labels to the
portfolio manager, and nothing else read it.

Bodies moved verbatim from src/pipeline_prompt_facts_review.py (`PromptFactsReviewMixin`),
which keeps same-named thin shims built per call. Every collaborator is an explicit
keyword-only constructor argument, so this builds and runs with no pipeline behind it.
"""


class ReviewExits:
    """The exits review boundary; standalone, built from explicit collaborators."""

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
