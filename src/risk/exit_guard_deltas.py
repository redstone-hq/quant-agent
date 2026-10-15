"""Metric deltas and the deterioration-claim veto, lifted verbatim from exit_guard.py."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field


#: these are claims about the position's OWN trajectory, which is exactly what
#: the stored metrics can adjudicate. Anything about the world (news, earnings,
#: regime, invalidation) is not here and is never vetoed by this module.
DETERIORATION_PATTERNS: tuple[str, ...] = (
    r"\bstall(?:ed|ing|s)?\b",
    r"\bnot progress(?:ing)?\b",
    r"\bno progress\b",
    r"\black of progress\b",
    r"\bfail(?:ed|ing|s)? to progress\b",
    r"\bgoing nowhere\b",
    r"\bdead money\b",
    r"\blosing momentum\b",
    r"\bmomentum (?:has )?fad(?:ed|ing)\b",
    r"\bdeteriorat(?:ed|ing|ion)\b",
    r"\bweaken(?:ed|ing)\b",
    r"\bslow(?:ing|ed)? (?:down|progress)\b",
    r"\bbehind schedule\b",
    r"\bstagnant\b",
    r"\bdrifting\b",
)

_DETERIORATION_RE = re.compile("|".join(DETERIORATION_PATTERNS), re.IGNORECASE)

#: Metrics where a HIGHER value means the position is doing better.
_HIGHER_IS_BETTER = ("thesis_progress_pct", "distance_to_stop_pct", "r_multiple", "pace")

#: The one metric in `_HIGHER_IS_BETTER` that is a function of the desk's
#: OWN protection as much as of the market.
#:
#: `distance_to_stop_pct = (current - stop) / current * 100`
#: (`TradingPipeline._build_position_facts`). Both terms move. Raise the
#: price and it improves — that is a real improvement. LOWER THE STOP and it
#: also improves, and nothing about the position got better; the desk just
#: took off some of its own protection.
#:
#: Verified on real recorded snapshots, 2026-09-01 midday vs the 2026-08-31
#: close (`specialist_evidence`, agent_name='position_reviewer',
#: kind='review_metrics'):
#:   V      distance-to-stop 1.42 -> 3.41 "improved" while r_multiple fell
#:          -0.11 -> -0.82. Price fell and distance rose, which is only
#:          possible if the stop moved down (entry 381.18 / stop 374.27 at
#:          entry; the implied stop by 09-01 midday is ~362.7).
#:   CMCSA  3.53 -> 5.58 "improved" while r_multiple fell -0.04 -> -0.33.
#:   DIS    1.85 -> 5.07 "improved" while thesis progress fell
#:          -3.06 -> -12.50.
#: In all three the position was demonstrably deteriorating and one of the
#: four "things improved" measures said otherwise.
#:
#: The fix is decomposition, not deletion — see
#: `MetricDeltas.stop_driven`. Deleting the metric would lose the genuine
#: and load-bearing case: a position whose PRICE has risen away from a
#: FIXED stop really has improved, and that is the case the metric was
#: added for (the 2026-08-26 EPD premature exit in this module's own
#: header cites distance-to-stop having improved).
_STOP_DEPENDENT_METRIC = "distance_to_stop_pct"

#: Snapshot fields that are not metrics but are needed to attribute a
#: `distance_to_stop_pct` move to the price or to the stop. Carried
#: alongside the metrics by `TradingPipeline._REVIEW_METRIC_KEYS`. `qty`
#: (2026-09-18 follow-up) supplies the SIDE: `distance_to_stop_pct` mirrors
#: its numerator for a short (`stop - current` instead of `current -
#: stop`), and the counterfactual recomputation below must mirror the same
#: way or it silently reproduces the pre-fix long-only formula for every
#: short reviewed.
_PROVENANCE_KEYS = ("stop_loss", "current_price", "qty")

#: Metrics scored against the name's own 14-day Wilder ATR (owner ruling,
#: 2026-10-15: a move smaller than one ATR is noise — a derived rule, not a
#: chosen multiplier). Both are price measures, so one ATR of price is the
#: noise band, expressed in each metric's own units by `MetricDeltas._floor`.
_ATR_SCORED = ("distance_to_stop_pct", "r_multiple")

#: Metrics shown but never scored improved/worsened. Both are measured
#: against the AI-asserted progress target (`pace` is
#: `thesis_progress_pct / time_fraction`), so no price noise band applies
#: and none is invented for them (the deleted ledger rows' own DELETE
#: outcome).
_UNSCORED = ("thesis_progress_pct", "pace")


def _finite(value: object) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class MetricDeltas:
    """Change in one position's metrics since the previous review."""

    symbol: str
    changes: dict[str, tuple[float, float]] = field(default_factory=dict)
    prior_timestamp: str | None = None
    #: `{stop_loss: (before, after), current_price: (before, after),
    #: qty: (before, after)}` when the snapshot pair carried them. Not
    #: metrics and never scored — they exist only to attribute a
    #: `distance_to_stop_pct` move (see `_STOP_DEPENDENT_METRIC`) and, via
    #: `qty`'s sign, to know which side's formula to replay. Empty for a
    #: snapshot written before 2026-09-18, which is handled explicitly
    #: rather than guessed.
    provenance: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: The name's 14-day Wilder ATR as % of the CURRENT price, read from the
    #: position facts (`atr_pct`, from `_atr_for_symbol`). None when bars
    #: were unavailable — the ATR-scored metrics are then unscored, never
    #: compared against a fallback constant.
    atr_pct: float | None = None

    @property
    def has_prior(self) -> bool:
        return bool(self.changes)

    def _floor(self, name: str) -> float | None:
        """One ATR of price in `name`'s own units; None means unscored.

        `distance_to_stop_pct` is % of price, so one ATR is `atr_pct`.
        `r_multiple` is linear in price at fixed lots, so one ATR in R is
        ATR x |dR / dPrice| over this snapshot pair; None when the pair
        carries no price move to read that slope from.
        """
        if name not in _ATR_SCORED or self.atr_pct is None or self.atr_pct <= 0:
            return None
        if name == "distance_to_stop_pct":
            return self.atr_pct
        pair = self.changes.get(name)
        prices = self.provenance.get("current_price")
        if pair is None or prices is None or prices[1] <= 0 or prices[1] == prices[0]:
            return None
        atr_dollars = self.atr_pct / 100 * prices[1]
        return atr_dollars * abs((pair[1] - pair[0]) / (prices[1] - prices[0]))

    def _distance_move_is_price_driven(self) -> bool | None:
        """Did `distance_to_stop_pct` improve because the PRICE moved?

        True  - it improves even holding the stop where it was.
        False - it only improves because the stop moved; the price alone
                does not carry it past the same noise floor.
        None  - unattributable: this pair of snapshots does not carry the
                stop and the price, or the recorded side flipped between
                the two snapshots, so the question cannot be answered.

        The test is the metric's own formula with the stop pinned to its
        prior value — an exact decomposition, not an estimate, and it
        introduces no number: it reuses the one-ATR floor (`_floor`)
        exactly as `improved` does. It must mirror
        `TradingPipeline._build_position_facts`'s side-aware formula
        exactly (2026-09-18 fix + follow-up), or it silently reproduces
        the pre-fix long-only arithmetic for every short reviewed —
        recomputing the wrong number is worse than not recomputing at all.
        """
        pair = self.changes.get(_STOP_DEPENDENT_METRIC)
        if pair is None:
            return None
        before_stop = self.provenance.get("stop_loss")
        prices = self.provenance.get("current_price")
        if before_stop is None or prices is None:
            return None
        stop_then = before_stop[0]
        price_now = prices[1]
        if price_now <= 0:
            return None
        # `qty` is the side. Missing (a snapshot pair that predates this
        # field, or a hand-built long-only fixture) defaults to LONG rather
        # than refusing to attribute: that reproduces this function's own
        # pre-existing behaviour exactly, which is correct for an actual
        # long and, for an actual short, merely continues for one more
        # review cycle the same long-only misattribution this whole fix
        # closes — never worse than the status quo it replaces, and it
        # self-heals once both snapshots carry `qty`.
        qty_pair = self.provenance.get("qty")
        if qty_pair is None:
            is_short = False
        else:
            qty_before, qty_after = qty_pair
            # A side flip between snapshots (long closed and a short opened
            # on the same symbol, or vice versa) means the "prior stop" is
            # not even the same position's stop. Refuse to attribute rather
            # than guess — same fail-safe posture as every other None here.
            if (qty_before < 0) != (qty_after < 0):
                return None
            is_short = qty_after < 0
        # Mirrors `dist_stop_pct` in `TradingPipeline._build_position_facts`
        # exactly: SHORT numerator is `(stop - current)` (stop sits above
        # price), LONG is `(current - stop)`. The stop is held at its
        # PRIOR value; only price is let move, to isolate its contribution.
        numerator = (stop_then - price_now) if is_short else (price_now - stop_then)
        counterfactual = numerator / price_now * 100
        floor = self._floor(_STOP_DEPENDENT_METRIC)
        if floor is None:
            return None
        return counterfactual - pair[0] > floor

    @property
    def stop_driven(self) -> list[str]:
        """Metrics whose apparent IMPROVEMENT is not the position improving.

        Today this is only ever `distance_to_stop_pct`, and only when the
        improvement survives on the arithmetic solely because the stop
        moved. `improved` excludes these, so loosening protection can no
        longer make a deteriorating position read as improving.

        An unattributable case (no stop/price in the snapshot pair) is
        listed here too. That is the conservative direction on this path:
        not counting an improvement can only make
        `veto_contradicted_exit` fire LESS, and a veto stranding the desk
        in a losing position is the worse failure (the call site's own
        disclosed reasoning).
        """
        if _STOP_DEPENDENT_METRIC not in self.changes:
            return []
        before, after = self.changes[_STOP_DEPENDENT_METRIC]
        floor = self._floor(_STOP_DEPENDENT_METRIC)
        if floor is None or after - before <= floor:
            return []  # not an apparent improvement at all
        return [] if self._distance_move_is_price_driven() else [_STOP_DEPENDENT_METRIC]

    @property
    def improved(self) -> list[str]:
        """Metrics that moved in the position's favour beyond the noise floor.

        Excludes a `distance_to_stop_pct` rise that came from the stop
        moving rather than the price moving (`stop_driven`) — the desk
        does not get to call loosening its own protection an improvement.
        """
        excluded = set(self.stop_driven)
        out = []
        for name, (before, after) in self.changes.items():
            floor = self._floor(name)
            if floor is None or name in excluded:
                continue
            if after - before > floor:
                out.append(name)
        return sorted(out)

    @property
    def worsened(self) -> list[str]:
        """Metrics that moved against the position beyond the noise floor.

        Deliberately NOT decomposed. A `distance_to_stop_pct` FALL can
        also be stop-driven (the stop was tightened), but discounting it
        would remove an entry from `worsened` and could turn
        `net_improved` True — making the veto fire more often and
        blocking an exit on paperwork. The asymmetry is the fail-open
        direction this path requires, not an oversight.
        """
        out = []
        for name, (before, after) in self.changes.items():
            floor = self._floor(name)
            if floor is None:
                continue
            if before - after > floor:
                out.append(name)
        return sorted(out)

    @property
    def net_improved(self) -> bool:
        """True when something improved and nothing measurably worsened.

        Deliberately strict. A mixed picture (progress up, distance-to-stop
        down) is a real judgment call and stays the reviewer's to make; only an
        unambiguous improvement contradicts a deterioration claim.
        """
        return bool(self.improved) and not self.worsened

    def render(self) -> str:
        """One line per changed metric, for the reviewer's prompt."""
        if not self.changes:
            return f"  {self.symbol}: no prior review on record (first look)"
        bits = []
        stop_driven = set(self.stop_driven)
        for name in sorted(self.changes):
            before, after = self.changes[name]
            arrow = "→"
            direction = ""
            floor = self._floor(name)
            if name in _UNSCORED:
                direction = " (unscored: measured against the AI's own target)"
            elif name in _HIGHER_IS_BETTER and floor is None:
                direction = " (unscored, no ATR)"
            elif name in _HIGHER_IS_BETTER:
                if name in stop_driven:
                    # Never shown to the reviewer as "(improved)". It read
                    # that way on real 2026-09-01 snapshots for V, CMCSA
                    # and DIS while all three were deteriorating.
                    attributed = self._distance_move_is_price_driven()
                    direction = (
                        " (wider stop, NOT an improvement)"
                        if attributed is False
                        else " (rose, provenance unknown — not counted)"
                    )
                elif after - before > floor:
                    direction = " (improved)"
                elif before - after > floor:
                    direction = " (worsened)"
                else:
                    direction = " (flat, within one ATR)"
            bits.append(f"{name} {before:.2f} {arrow} {after:.2f}{direction}")
        stamp = f" since {self.prior_timestamp}" if self.prior_timestamp else ""
        return f"  {self.symbol}{stamp}: " + " | ".join(bits)


def compute_deltas(
    symbol: str,
    prior: dict | None,
    current: dict | None,
    prior_timestamp: str | None = None,
) -> MetricDeltas:
    """Metric-by-metric change, skipping anything missing on either side.

    `atr_pct` is read from `current` only — the live ATR is today's noise
    band, and older snapshots never persisted it.
    """
    changes: dict[str, tuple[float, float]] = {}
    provenance: dict[str, tuple[float, float]] = {}
    if prior and current:
        for name in _HIGHER_IS_BETTER:
            before = _finite(prior.get(name))
            after = _finite(current.get(name))
            if before is None or after is None:
                continue
            changes[name] = (before, after)
        # Stop/price/qty are carried, never scored — they only answer "did
        # the distance-to-stop move because the market moved or because we
        # moved the stop, and on which side?". Absent on snapshots written
        # before 2026-09-18.
        for name in _PROVENANCE_KEYS:
            before = _finite(prior.get(name))
            after = _finite(current.get(name))
            if before is None or after is None:
                continue
            provenance[name] = (before, after)
    return MetricDeltas(
        symbol=symbol.upper(),
        changes=changes,
        prior_timestamp=prior_timestamp,
        provenance=provenance,
        atr_pct=_finite((current or {}).get("atr_pct")),
    )


def is_deterioration_claim(reason: str) -> bool:
    """True when `reason` asserts the position's own trajectory is worsening."""
    return bool(reason) and bool(_DETERIORATION_RE.search(reason))


def veto_contradicted_exit(
    action: str,
    reason: str,
    deltas: MetricDeltas,
) -> str | None:
    """Return a veto message when an exit contradicts its own numbers, else None.

    Vetoes only when ALL of these hold:
      - the action actually reduces the position (SELL / REDUCE / COVER —
        COVER is the short-side twin: it reduces/closes a SHORT exactly as
        SELL/REDUCE reduce/close a LONG),
      - the stated reason is a deterioration claim about the position itself,
      - a prior snapshot exists to compare against,
      - and every metric that moved, moved in the position's favour.

    Everything else passes through untouched. In particular a SELL/COVER
    citing news, earnings, a regime shift or a triggered invalidation is
    never vetoed here however good the numbers look — those are exits on
    new information, which the reviewer keeps full authority to make (spec
    Phase 3.8).

    `deltas` is trusted to already be direction-corrected for a short, so
    COVER needs no separate sign handling in this function. Verified
    per-metric, 2026-09-18 (each is computed in
    `TradingPipeline._build_position_facts` unless noted):
      - `thesis_progress_pct` — side-correct by construction: both
        `(cur - entry)` and `(progress_target - entry)` flip sign together
        for a short, so the ratio is unchanged.
      - `pace` — inherits `thesis_progress_pct`'s correctness; the
        denominator (`time_fraction`) is never signed.
      - `r_multiple` — side-correct by construction (`src/risk/metrics.py
        ::position_risk` takes `qty`'s sign as the side; its P&L numerator
        mirrors for a short and its per-lot risk denominator is positive).
      - `distance_to_stop_pct` — was NOT side-correct until 2026-09-18: it
        was computed as `(cur - stop_loss) / cur * 100` regardless of side,
        which is correct for a long (stop below price) but for a short
        (stop above price) is negative and moves the WRONG way — it gets
        MORE negative, i.e. reads as "worse" under `_HIGHER_IS_BETTER`, as
        the price moves further from the stop and the position gets safer.
        Fixed by mirroring the numerator for `qty < 0`. Any snapshot
        written before that fix still has old-formula (mis-signed for
        shorts) values, so a delta spanning that boundary is stale, not
        wrong — it self-heals after one review cycle.
    """
    if str(action).upper() not in ("SELL", "REDUCE", "COVER"):
        return None
    if not is_deterioration_claim(reason):
        return None
    if not deltas.has_prior or not deltas.net_improved:
        return None
    moved = ", ".join(f"{name} {deltas.changes[name][0]:.2f}→{deltas.changes[name][1]:.2f}" for name in deltas.improved)
    return (
        f"{deltas.symbol}: {action} vetoed — the reason claims the position is "
        f"deteriorating, but every metric that moved since the previous review "
        f"improved ({moved}). A deterioration verdict may not contradict the "
        f"reviewer's own recorded numbers. Exit on new information (news, "
        f"earnings, regime, invalidation) is unaffected."
    )
