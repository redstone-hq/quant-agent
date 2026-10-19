"""Missing evidence — a filing the desk never read.

Deliberately NOT the conviction bar: a block for evidence that was never
looked at, not for evidence that was weak.

Bodies moved VERBATIM from `src/risk/rules.py` (AST-identical to the
originals; `tests/test_risk_rules_parts_boundary.py` is the witness that this
part builds and runs alone). `src/risk/rules.py` keeps the engine, every
ledger-pinned number and the re-export mirror, so every existing
`from src.risk.rules import X` keeps resolving.
"""
# --- Missing evidence: a filing the desk never read -------------------------
#
# NOT THE CONVICTION BAR, AND DELIBERATELY NOT ROUTED THROUGH IT (board item
# 186, 2026-10-01). The R7 bar below grades what the seats SAID: one
# name-level seat with a real directional thesis and the chart confirming
# (an opposed name-level seat is an objection to answer, desk archetype
# 19 Oct 2026). A queued-but-unread filing is none of those things — it is a
# seat that was never asked, and `agreement_refuses_trade` above names
# exactly that distinction ("the seats disagreed" vs "the seats had nothing
# to look at") as the confusion it exists to avoid. Treating an unread filing
# as a conviction failure would also borrow `OWN_BAR_REASON_PREFIX`, which
# `src/rotation.py` STRING-MATCHES to classify HELD names as ineligible to
# hold, so it would silently change behaviour on positions the desk already
# owns. This route is MISSING EVIDENCE, it carries its own prefix, and
# nothing matches that prefix anywhere.

#: The reason prefix for an entry refused because evidence the desk meant to
#: have was not fetched. Matched by nothing — deliberately.
UNREAD_FILING_REASON_PREFIX = "Unread filing"


def unread_filing_block_reason(symbol: str) -> str:
    """The one refusal string for a BUY whose just-filed report was never read.

    WHAT THE TRIGGER ACTUALLY MEANS, because it changes the argument. The
    `queued=True` placeholder is set in exactly one place (`src/pipeline.py`,
    the session-time earnings fetch): a filing that turns up as NEW at
    decision time, i.e. one the pre-market preprocess did not pick up and
    analyse. It marks an OPERATIONS FAILURE — a step of this desk's own
    pipeline did not run or did not finish in time — not a market event and
    not a seat's verdict.

    So this refusal is argued on evidence, not on conviction: the desk meant
    to read that report before deciding, it did not, and it declines to buy
    into the gap rather than buying on an incomplete picture. Holding is
    untouched; this refuses an ENTRY only.

    This replaced the 5%-of-book weight clamp (board item 186, 2026-10-01).
    That 5 had no source and two derivations failed, and the owner ruled on
    2026-09-30 that such a constant is a defect to remove rather than an
    appetite to answer — so the condition was reformulated and the number
    deleted instead of re-derived.
    """
    return (
        f"{UNREAD_FILING_REASON_PREFIX} \u2014 {symbol.strip().upper()}: a "
        "just-filed report reached this session unread because the pre-market "
        "preprocess did not analyse it, so the desk is deciding without "
        "evidence it meant to have; entry refused until it is read"
    )
