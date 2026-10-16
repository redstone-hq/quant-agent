"""Notes the PM briefing prints under the evidence registry (lifted out of
the agent's briefing builder, wording unchanged)."""

from __future__ import annotations


def stale_registry_note(stale_sources, evidence_registry) -> str:
    """The STALE note, or "" when it would list no symbol."""
    # The registry values themselves stay undecorated — the PM must
    # copy the stance string EXACTLY for `validate_grounding`, so the
    # staleness is carried alongside rather than inside them.
    note = (
        "\n\nSTALE (still real coverage, still citable as provenance, "
        "but NOT counted toward the agreement score below — the "
        "filing is no longer the company's latest periodic report, or the "
        "next report's legal filing deadline has passed):\n"
        + "\n".join(
            f"- {symbol}: {', '.join(sorted(sources))}"
            for symbol, sources in sorted(stale_sources.items())
            if symbol in evidence_registry
        )
    )
    return "" if note.rstrip().endswith(":") else note


def broadcast_registry_note(non_corroborating_sources, evidence_registry) -> str:
    """The MARKET-WIDE note, or "" when it would list no symbol."""
    # A DIFFERENT fact with a different consequence, so it gets its
    # own note rather than an "or" the reader cannot resolve: this
    # stance is current and real, it simply is not about this name.
    note = (
        "\n\nMARKET-WIDE, NOT ABOUT THIS NAME (still real coverage, "
        "still citable as provenance, and still counted AGAINST a "
        "trade it opposes — but it can never count FOR one: this "
        "macro stance is the broad equity outlook, applied to a name "
        "whose sector the macro read did not mention):\n"
        + "\n".join(
            f"- {symbol}: {', '.join(sorted(sources))}"
            for symbol, sources in sorted(non_corroborating_sources.items())
            if symbol in evidence_registry
        )
    )
    return "" if note.rstrip().endswith(":") else note


def omitted_rows_line(omitted: int, n_broadcast: int, n_stale: int) -> str:
    """The one line that COUNTS the all-zero agreement rows left out.

    MEASURED 2026-10-04 on the recorded production briefing: EVERY
    all-zero row carries the identical broadcast boilerplate, so a
    caveat filter would have dropped nothing. The caveat is not a
    per-row fact — the sentence is byte-identical on each of them and
    the note it points at is already printed once under the block —
    so what it conveys is carried by the counts on the summary line.
        MEASURED 2026-10-04 on a recorded production briefing: 38 of
        the 82 rows read `0 aligned / 0 opposed` on BOTH sides with no
        caveat attached — 6,768 of 87,234 characters (7.8%) carrying no
        fact at all, at the seat that is 91% of model spend. They are
        omitted here and COUNTED on one line below, so the model can
        never read an omission as the symbol being absent. A row with a
        stale or broadcast caveat is NOT empty and is always kept.
    """
    breakdown = ""
    if n_broadcast:
        breakdown += (
            f" {n_broadcast} of them have only a one-sided "
            "broadcast macro stance, which cannot count FOR a trade "
            "— see the note below."
        )
    if n_stale:
        breakdown += f" {n_stale} of them have only a stale stance, counted neither way."
    return (
        f"- ({omitted} further symbol(s) are present in "
        "the registry above but have no aligned and no opposed source "
        "on either side — net +0 long and net +0 short — so their rows "
        "are omitted here; omitted does NOT mean absent."
        f"{breakdown})"
    )
