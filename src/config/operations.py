"""Desk-operations settings: schedule, trading sessions, storage, prompt evolution, reconciliation, deployment gap.

Moved verbatim from src/config/__init__.py (pure move; bodies AST-identical).
"""

from pydantic import BaseModel, Field


class ScheduleConfig(BaseModel):
    earnings_preprocess: str = "08:00"
    morning: str
    intra_check: str = "10:30"
    midday: str
    close: str = "15:30"
    evening: str


class TradingConfig(BaseModel):
    # Universe must be non-empty — empty list silently produces zero
    # data, zero analyses, zero trades for the whole session. Catch
    # at config load instead of letting it surface as a degraded
    # day with no obvious cause.
    universe: list[str] = Field(min_length=1)
    # Lookback for OHLCV bars feeding the technical indicators. Negative
    # or zero values used to load silently and fail downstream with
    # opaque pandas slicing errors. Floor at 1 (one day of bars is
    # the absolute minimum for any indicator).
    lookback_days: int = Field(ge=1)
    schedule: ScheduleConfig


class StorageConfig(BaseModel):
    # Empty would leave the owner pause switch with no database to read.
    db_path: str = Field(min_length=1, pattern=r"\S")


class EvolutionConfig(BaseModel):
    """Quarterly meta-reflection prompt-evolution settings.

    `enabled=False` is the safe default — PR3 (the meta_reflector) writes
    reflection.json to disk but the editor never runs. Flip to True only
    after reviewing a quarter or two of reflection.json contents by hand.
    Every guard below is redundantly enforced in src/evolution/prompt_editor.py;
    this block makes them tunable per deployment.
    """

    enabled: bool = False
    """Master switch. PR4 default is False — the editor stays dormant
    until explicitly flipped. Flipping back to False does not retract
    already-applied learnings; use the retract path in the reflector."""

    auto_commit: bool = True
    """After successful prompt edits, `git add` + `git commit` each
    modified prompt file so `git revert <hash>` provides a one-shot
    rollback for a whole quarter's evolution. Only meaningful when
    `dry_run=False`."""

    dry_run: bool = True
    """Default True for safety. When True, `PromptEditor.apply_reflection`
    does NOT modify any prompt file — instead it writes the proposed
    edits to `data/evolution/{period}/proposed_edits.json` for human
    review. To actually apply a quarter's proposals, flip `dry_run` to
    False temporarily and re-run `python main.py --mode meta --force`,
    OR edit the prompt files by hand using the JSON as a reference.

    Reason this defaults True (audit H3 follow-up): meta-reflection
    auto-fires from evening on quarter-end (added in Round 2). A bad
    learning landing as an auto-commit is silently degrading — affects
    every decision until next quarter or until operator notices via git
    log. The 4 gates (FIFO cap / Jaccard dedup / prohibited-words regex
    / agent allowlist) catch obvious bad learnings but not subtle
    polarity-flipped polite proposals. Keep dry_run=True for the first
    2-3 quarters; once the proposals track operator's expectations,
    flip to False."""

    max_agents_per_cycle: int = 3
    """Hard cap — at most N agents get edited per quarterly run even if
    the meta-reflector proposes more. Schema cap on proposed_learnings
    is already 3; this is the second belt."""

    max_learnings_per_agent: int = 10
    """FIFO buffer per agent prompt. When an append would push past the
    cap, the oldest auto-added entry (by date-tag, not manual) is
    rolled off before the new one is appended."""

    max_learning_chars: int = 200
    """Upper bound per entry. Schema enforces ≥20 already; this is the
    ≤200 end. Prevents prompt bloat."""

    min_justification_chars: int = 40
    """Schema floor on PromptLearning.justification. Echoed here so a
    deployment can tighten it (the schema's 40 is the loosest allowed)."""

    jaccard_dedup_threshold: float = 0.6
    """Token-level Jaccard similarity against EACH existing entry in
    the target agent's Learnings section. If any pair exceeds this,
    the new entry is treated as a near-duplicate and rejected.
    0.6 tuned loose — catches paraphrases without rejecting legitimately
    similar-topic learnings written differently."""

    prohibited_words: list[str] = Field(
        default_factory=lambda: [
            "never",
            "always",
            "override",
            "ignore all",
            "must always",
            "must never",
        ],
    )
    """Case-insensitive word-boundary regex check on learning_text. These
    directly conflict with invariant wording in the core prompts (e.g.
    RM's 'ALWAYS require stop_loss'); letting an LLM append a 'never' rule
    can flip the hard discipline."""

    protected_agents: list[str] = Field(
        default_factory=lambda: ["risk_manager", "position_reviewer"],
    )
    """Agents whose prompts the editor MUST NOT touch. The Pydantic
    MetaReflectionAgentName literal already excludes these — this is
    the second belt at the editor layer."""


class ReconciliationConfig(BaseModel):
    """Broker-truth reconciliation of the `trades` ledger against Alpaca.

    2026-08-28 ONDS/CCJ incident: both positions were closed by their
    broker-resident protective stop (a GTC stop-limit order placed by
    `AlpacaBroker.place_entry_protection` / `_repair_stop_coverage` /
    `shift_stops_down`, none of which ever wrote a `trades` row for the
    stop order itself). The stop fired, the position vanished from the
    broker, and the ledger never heard about it — the BUY rows sat forever
    at `realized_pnl IS NULL` and the `positions` table (synced directly
    from broker truth) quietly diverged from the story `trades` told.
    `_reconcile_stop_out_fills` (src/pipeline.py) closes that gap by
    diffing the ledger's own implied share count against the broker's
    actual position and pulling any untracked filled SELL order it finds.
    """

    stop_out_lookback_days: int = Field(default=7, ge=1, le=60)
    """How far back to ask the broker for filled SELL orders when the
    ledger believes a symbol is still (partly) held but the broker shows
    less. Wide enough to survive a multi-day outage of the reconciler
    itself (weekends + a stuck timer) without being so wide it makes the
    per-session broker query expensive. Alpaca's own order-history
    retention is the real outer bound this can't exceed."""
