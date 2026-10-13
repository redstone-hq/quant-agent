import math
from dataclasses import dataclass

# --- Spec §11.2 — gross exposure, its ceiling, and the de-levering ladder --
#
# WHAT DID NOT EXIST BEFORE THIS SECTION: any gross-exposure ceiling at all.
# `max_portfolio_risk_pct` (25) bounds capital AT RISK — the sum of stop
# distances. It is not a bound on how much the book OWNS, and nothing stopped it
# reaching the broker's 4x. Everything below is therefore a TIGHTENING.
#
# One definition, several consumers, deliberately — the same discipline
# §12.2 imposed on sector exposure after three divergent implementations of
# "how much is this sector holding" let a signed-vs-gross defect survive.
# `gross_exposure` is the ONLY place gross is measured. `resolve_gross_ceiling`
# is the ONLY place the ladder is read. `apply_gross_ceiling` is the ONLY
# place the ceiling is enforced, and it is what fixes the ORDER of the two
# responses (block first, trim second) so that ordering cannot be got wrong
# by a caller.

#: Peak-to-trough drawdown rungs, shallowest first: at or worse than
#: `threshold`, gross exposure may not exceed `ceiling_x` times equity.
#: The owner's ratified table (2026-09-01):
#:
#:     0% to  -8%      ->  2.0x   (the standing cap, no rung fires)
#:    -8% to -15%      ->  1.5x
#:   -15% to -20%      ->  1.0x
#:   worse than -20%   ->  0.5x, and the owner is alerted
#:
#: The 2.0x row is the CONFIGURED cap (`risk.max_gross_exposure_x`), not a
#: rung — the ladder can only ever tighten it, never raise it, so an operator
#: who lowers the setting lowers every rung with it.
#:
#: **This is now the desk's ONLY account-level drawdown response**
#: (2026-09-20, owner instruction, docs/WORK.md item 32 retired). The desk
#: used to carry a second, separate mechanism alongside it: a daily
#: circuit breaker that halted all new trading on the day's loss, and
#: 5-day / 20-day rolling-return "drawdown brakes" that halved every new
#: BUY and SHORT. Both were removed in full. The owner's reason, verbatim:
#: "I'm starting to think that I'm fine with the stops on the individual
#: stocks and I do not want a nuclear option so remove the whole secondary
#: halt on portfolio completely because there's too many things you keep
#: finding where a slight normal fluctuation in the market can liquidate or
#: halt everything — that's too dangerous to leave — the proper stop losses
#: should be enough."
#:
#: This ladder was deliberately left untouched by that removal, and its
#: character is why: it lowers gross exposure in steps at -8% / -15% /
#: -20% peak-to-trough and it never halts the desk. The 0.5x floor is on the
#: CEILING, not on what is held: each forced cut sells a whole position, so a
#: book of one or two names can be sold all the way to cash. When it does
#: force the book down it sells
#: WHOLE positions — losers first, biggest loss first, then the
#: weakest-rated winner — never part of one (owner rulings 2026-10-09 and
#: 2026-10-12; `apply_gross_ceiling`). It is a ratified owner table about
#: how much the book may OWN, not a loss alarm. Do not reintroduce a halt or a sizing brake
#: on the strength of this table.
GROSS_LADDER: tuple[tuple[float, float], ...] = (
    (-8.0, 1.5),
    (-15.0, 1.0),
    (-20.0, 0.5),
)

#: At or worse than this drawdown the OWNER IS TOLD. Kept as its own
#: constant rather than inferred from the deepest `GROSS_LADDER` row, because
#: it answers a DIFFERENT question from the ladder: "at what drawdown must
#: the owner hear about it", not "how much may the book own". The same
#: function proves the two are separate — `alert_owner=True` is also set on
#: the unmeasurable-drawdown branch, where no rung fires at all.
#:
#: A 2026-09-30 change (board item 182) briefly replaced this with
#: `min(threshold for threshold, _ in GROSS_LADDER)` and was REVERTED the
#: same day, because it made the desk QUIETER, the opposite of the intent.
#: The trigger below is `drawdown <= GROSS_LADDER_ALERT_PCT`, which is
#: MONOTONE: once the drawdown passes the threshold the alert is true at
#: every deeper drawdown too. Freezing this at -20 therefore cannot produce
#: silence anywhere — add a -30 rung and the owner is still told from -20
#: onward, including at -30. Tying it to the deepest rung instead MOVES the
#: alert down to -30 and buys real silence between -20% and -30%, a band in
#: which the ladder is actively cutting the book to 0.5x.
#:
#: What was actually wrong was the SENTENCE, not the trigger. With a deeper
#: rung present, a -22% message reading "the desk is at its most de-levered
#: setting" is untrue. That is prose rot and it is fixed where it lives: the
#: reason string below names the rung the ladder is ACTUALLY on, and only
#: claims the floor when the book is at the floor.
#:
#: SOURCED 2026-09-30 (board item 182). The value is no longer the desk's own
#: round number. It is the depreciation-notification threshold published in
#: Article 62(1) of Commission Delegated Regulation (EU) 2017/565 (the MiFID
#: Org Regulation), reproduced in the FCA Handbook as COBS 16A.4.3UK: a firm
#: managing a portfolio "shall inform the client where the overall value of
#: the portfolio ... depreciates by 10 % and thereafter at multiples of 10 %,
#: no later than the end of the business day in which the threshold is
#: exceeded". That rule answers THIS question and no other one this desk
#: could find: at what loss must the person whose money it is be told, quite
#: apart from anything the manager does to the book. It is therefore adopted
#: as the trigger, and the alert moves from -20% to -10%.
#:
#: HONEST DIFFERENCES, stated rather than papered over. (a) The regulation
#: measures depreciation against the value at the START of the reporting
#: period; this desk measures peak-to-trough drawdown. Peak-to-trough is
#: always at least as deep as period-start depreciation, so alerting on it at
#: -10% fires no later than the regulation would, never later. (b) The
#: regulation is a RETAIL investor-protection rule and the UK FCA revoked
#: COBS 16A.4.3UK with effect from 23 October 2025; the revocation was a
#: firm-burden decision, not a finding that 10% is the wrong number, and the
#: EU Article 62 text stands. It is cited here as published practice, not as
#: a rule this desk is subject to. (c) The desk re-alerts on every session
#: past the threshold, which is more often than "thereafter at multiples of
#: 10%" requires; the regulation is a floor on loudness and this clears it.
#:
#: This direction is the one the 2026-09-30 revert argued for: the change
#: makes the desk LOUDER. It cannot introduce silence anywhere, because the
#: trigger is monotone and -10% is shallower than the old -20%.
GROSS_LADDER_ALERT_PCT = -10.0


@dataclass(frozen=True)
class GrossCeiling:
    """The resolved gross-exposure ceiling for one session.

    Computed from ACCOUNT STATE ONLY — equity, its high-water mark, and the
    configured cap. Nothing the Portfolio Manager produced is an input, and
    it has a correct value on a run where the PM returned nothing at all.
    That is not incidental: a blank PM response is a measured failure mode
    (one candidate model truncated mid-JSON on 1 run in 10), and a ceiling
    that depended on a parseable book would leave the desk fully levered at
    exactly the moment it should be shedding exposure.
    """

    ceiling_x: float
    base_x: float
    drawdown_pct: float | None
    alert_owner: bool
    rung: str
    reason: str

    @property
    def de_levered(self) -> bool:
        """True when a ladder rung has tightened the configured cap."""
        return self.ceiling_x < self.base_x


def resolve_gross_ceiling(
    drawdown_pct: float | None,
    *,
    base_x: float,
) -> GrossCeiling:
    """The de-levering ladder. A pure function of drawdown — apply it twice
    and you get the same answer, because a ceiling is a LEVEL, not a
    multiplier that compounds.

    That property is the whole defence against double-application. The PM
    prompt tells the model the ladder is the engine's arithmetic and never
    its own; if a future change applied a *multiplier* at two gates the book
    would de-lever twice as hard as intended. A level enforced twice is the
    same level.

    **Boundary rule: ties go to the TIGHTER rung.** A drawdown of exactly
    -8.00% resolves to 1.5x, not 2.0x. The ratified table's ranges touch at
    their endpoints, and fail-closed is the house rule everywhere else in
    this file.

    **Unknown drawdown is NOT treated as the deepest rung.** A fresh account
    with no equity history genuinely has no drawdown; forcing it to 0.5x
    would refuse every trade on day one and force-liquidate a book that never
    fell. It resolves to the configured cap, which is itself a real ceiling,
    and `apply_gross_ceiling` refuses to TRIM on an unmeasurable book. This
    matches how `_compute_recent_performance` has always treated an empty
    `daily_pnl` table.

    **But unknown is no longer SILENT (2026-09-18).** Holding the loosest
    cap was never the defect; doing it without telling anyone was. An
    unmeasurable drawdown now sets `alert_owner=True`, so it reaches the
    owner through the same leverage-line path `GROSS_LADDER_ALERT_PCT`
    already uses. This is the direct analogue of `apply_gross_ceiling`'s
    `measurable = False` branch, which likewise trims nothing and says so
    loudly rather than proceeding as if the book were fine. The ceiling
    itself is deliberately unchanged — tightening it to a rung would be
    picking a number for a state in which, by definition, nothing has been
    measured, and would force-liquidate the fresh-account case the paragraph
    above exists to protect.
    """
    base = float(base_x) if isinstance(base_x, (int, float)) and base_x > 0 else 0.0
    if not math.isfinite(base) or base <= 0:
        base = 0.0
    if drawdown_pct is None or not math.isfinite(drawdown_pct):
        return GrossCeiling(
            ceiling_x=base,
            base_x=base,
            drawdown_pct=None,
            alert_owner=True,
            rung="unknown",
            reason=(
                f"No measured equity history, so no drawdown could be "
                f"computed. Gross exposure is held to the standing "
                f"{base:.1f}x ceiling and nothing is trimmed on an "
                f"unmeasured book — but the de-levering ladder cannot "
                f"de-lever while this lasts, so the owner is being told."
            ),
        )
    drawdown = float(drawdown_pct)
    ceiling = base
    rung = "none"
    for threshold, rung_x in GROSS_LADDER:
        if drawdown <= threshold:
            ceiling = min(ceiling, rung_x)
            rung = f"{threshold:.0f}%"
    alert = drawdown <= GROSS_LADDER_ALERT_PCT
    if ceiling >= base:
        reason = (
            f"The book is {abs(drawdown):.1f}% below its equity high — inside "
            f"the {abs(GROSS_LADDER[0][0]):.0f}% band where no de-levering "
            f"applies. Gross exposure may reach {ceiling:.1f}x equity."
        )
    else:
        reason = (
            f"The book is {abs(drawdown):.1f}% below its equity high. The "
            f"de-levering ladder cuts the gross-exposure ceiling from "
            f"{base:.1f}x equity to {ceiling:.1f}x until the account recovers."
        )
    if alert:
        # Name the rung the ladder is ACTUALLY on. The alert threshold and
        # the ladder's deepest rung are separate numbers that happen to agree
        # today; if a deeper rung is ever added, this sentence must not go on
        # claiming the floor at a drawdown that is merely past the alert.
        deepest = min(threshold for threshold, _ in GROSS_LADDER)
        reason += f" Past the {abs(GROSS_LADDER_ALERT_PCT):.0f}% owner-alert level."
        floor_x = min(rung_x for threshold, rung_x in GROSS_LADDER if threshold == deepest)
        if drawdown <= deepest:
            reason += (
                f" The book is on the ladder's deepest {abs(deepest):.0f}% "
                f"rung — its most de-levered setting, {ceiling:.1f}x."
            )
        elif rung == "none":
            reason += (
                f" The ladder has not cut anything yet: the first rung is at "
                f"{abs(GROSS_LADDER[0][0]):.0f}% and the deepest, "
                f"{floor_x:.1f}x, is at {abs(deepest):.0f}%."
            )
        else:
            reason += (
                f" The ladder has the book on its {rung} rung at "
                f"{ceiling:.1f}x — NOT its most de-levered setting; a deeper "
                f"{abs(deepest):.0f}% rung at {floor_x:.1f}x still sits "
                f"below this."
            )
    return GrossCeiling(
        ceiling_x=ceiling,
        base_x=base,
        drawdown_pct=drawdown,
        alert_owner=alert,
        rung=rung,
        reason=reason,
    )
