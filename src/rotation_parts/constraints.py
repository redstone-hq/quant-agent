"""Rotation binding-constraint helpers (lifted verbatim from src/rotation.py)."""

from __future__ import annotations


def rotation_binding_constraints(
    *,
    headroom_pct: float,
    floor_pct: float,
    funding=None,
) -> tuple[str, ...]:
    """Which of the desk's limits currently stop it taking a new position.

    Two, and the union of them is the precondition (see the module
    docstring for why the union and not the binding one alone):

      * `risk_budget` — `allocate_risk_budget`'s headroom against the
        EXISTING book is already under the minimum this desk will size a
        new idea at. This is the original test, unchanged and still in
        force; it has simply stopped being the only one.
      * `funding` — the dollars `_entry_deployment_budget` says may still
        be deployed will not fund even the smallest position that can
        carry the owner's minimum risk per position. That one
        figure already carries the §11.2 gross ladder, settled cash, and
        the min of the two when margin is disabled, so a single test covers
        both the ladder and the cash constraint without this module
        computing either.

    `funding` is `src.risk.min_risk.min_risk_shortfall`'s check of the
    deployable dollars as a position at the best candidate's own stop. `None`,
    or an unreadable check, means the funding view was
    not resolvable this session; the funding test is then simply absent
    rather than guessed at, exactly as `existing_risk_pct=None` already
    disables the risk test. Silence from an unreadable input must not read
    as "there is room" NOR as "the book is full".
    """
    binding: list[str] = []
    if headroom_pct < floor_pct:
        binding.append("risk_budget")
    if funding is not None and funding.below_floor:
        binding.append("funding")
    return tuple(binding)


def funding_view_measured(
    entry_budget_usd: float | None,
    min_entry_usd: float | None,
) -> bool:
    """Was the funding constraint actually READ this session?

    2026-09-23, adversary review. `rotation_binding_constraints` returning
    `()` means "nothing is binding", and the prompt, the owner's report and
    the refusal row all render that as "there is room". When the funding
    figure was never resolvable, `()` would therefore have ASSERTED cash and
    borrowing room from a number nobody read — and the direction is adverse,
    because `_entry_deployment_budget` reports `ladder_backed=False` exactly
    when the gross ceiling is unresolvable, which is the branch where
    execution falls back to raw settled cash and is MOST constrained.

    So the three renderers ask this and say "not measured" rather than "there
    is room". The fallback has never fired in the retained logs (zero
    occurrences of the ladder-unreadable warning), which under this desk's
    own rule is a reason to make it safe, not a reason to trust it.
    """
    return (
        isinstance(entry_budget_usd, (int, float))
        and not isinstance(entry_budget_usd, bool)
        and isinstance(min_entry_usd, (int, float))
        and not isinstance(min_entry_usd, bool)
    )


def holdings_below_entry_bar(
    blocked: dict[str, list[str]],
    held_symbols: set[str],
) -> tuple[str, ...]:
    """Which currently-held names would NOT be bought today.

    Owner mandate (2026-09-23): "every stock must keep earning its right to
    be in the portfolio." A held name that now appears in `blocked` — i.e.
    fails the desk's own `candidate_eligibility` entry bar (R2 rating / R3
    BUY-eligible / R5 net evidence / R6 constructor-refused) — has, by that
    identical rule a brand-new buy must clear, stopped earning its place. It
    is the exact set the categorical rotation tier ("ineligible_hold") is
    allowed to prune from.

    This returns the WHOLE set, upper-cased and sorted, as a session-level
    telemetry fact — separate from `evaluate_rotation`'s choice of the ONE
    name to surface. It is recorded every session (see `precheck_record`) so
    the desk can measure how often, and on how many names, a holding has
    decayed below its own entry bar while still on the book — a number
    nothing recorded before, and the one that says whether "natural
    selection" has anything to act on at all. It is a COUNT of a categorical
    membership, not a score or a threshold, so it introduces no arbitrary
    number (board item 39(a) stays untouched).
    """
    held = {str(s).strip().upper() for s in held_symbols if str(s).strip()}
    return tuple(sorted(sym.upper() for sym, reasons in blocked.items() if reasons and sym.upper() in held))
