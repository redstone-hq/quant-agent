"""The minimum-ratchet floor: when a proposed stop is a DIFFERENT stop.

Split out of `src/risk/trailing.py` so the facade keeps only the contract.
Everything here is read off the instrument, never picked.
"""

from __future__ import annotations

__all__ = ["MIN_RATCHET_TICKS", "venue_tick", "min_ratchet_floor"]

#: A proposed stop must sit at least this far above the live stop. Mirrors the
#: reviewer's historical ">= 1.02x old stop" min-bump rule so the deterministic
#: path does not churn orders the discretionary one would have skipped.
#:
#: **2026-09-30: an attempt to re-express this as a reading off the instrument
#: FAILED, and the constant stays at 2.0 with the failure recorded.** The desk's
#: standing doctrine bars a flat picked percentage on a stop or exit, and this
#: one is squarely in scope — so the attempt was made, measured, and is written
#: down here rather than quietly abandoned.
#:
#: The complaint is real and is now MEASURED, not asserted. Expressing "2% of
#: the live stop" in each name's own ATR(14), over the six positions this gate
#: actually refused in production between 2026-09-21 and 2026-09-29:
#:   MRVL 0.31 ATR | NET 0.33 ATR | RKLB 0.34 ATR | AMD 0.47 ATR
#:   META 0.52 ATR | AAPL 0.91 ATR
#: The same nominal rule demands a ratchet nearly three times larger on AAPL
#: than on MRVL. That is exactly the incoherence the doctrine names.
#:
#: The natural repair is `k * ATR`, the unit this module already uses for
#: `NOISE_BAND_ATR_MULTIPLE` and `CHANDELIER_ATR_MULTIPLE`. It was measured
#: against the same production record — the seven refused tightens whose
#: candidate could be reconstructed from daily bars — and it does not work:
#:   * every refused tighten fell between 0.12 and 0.50 ATR;
#:   * any k >= 0.75 blocks ALL SEVEN, strictly MORE than the flat 2% blocks
#:     (which lets one through), so the change would tighten the gate, not
#:     loosen it;
#:   * only k <= 0.5 lets anything through, and choosing 0.25 to admit three
#:     of seven is fitting a constant to the outcomes the data happened to
#:     like. That is barred outright, and it is the same failure mode as the
#:     2.0 it would replace — a picked multiple re-imported through the ATR
#:     door.
#: No published work fetched fixes a minimum stop-adjustment size; the
#: literature on stop placement addresses DISTANCE from price (which is what
#: `NOISE_BAND_ATR_MULTIPLE` and the chandelier already answer), not the
#: minimum INCREMENT worth replacing a resting order for.
#:
#: Deleting the gate instead was considered and rejected on a measured cost,
#: not a preference: `AlpacaBroker.replace_stop_loss` cannot edit an Alpaca
#: OTO stop leg in place, so every replace is a cancel-then-resubmit with a
#: real window in which the position carries no protective order. Removing
#: the gate would have added seven such windows across nine evaluation runs
#: on an eleven-name book. The money cost of a replace is zero (the ledger
#: entry establishes this); the naked-window cost is not.
#:
#: That rejection was CONTINGENT and the contingency HAS NOW BEEN MET:
#: `replace_stop_loss` (`src/execution/broker_parts/stop_place.py`) now
#: PREFERS `_amend_resting_stop_price`, falling back to cancel+resubmit only
#: when the position re-read fails or the order is not amendable. The cost
#: that was the ONLY defence of this gate no longer applies on that path, so
#: the honest floor is one venue tick (SEC Rule 612 / Alpaca's $0.01 at or
#: above $1, $0.0001 below, already in `_quantize_price`).
#: (ledger: `MIN_RATCHET_TICKS`).
#:
#: What the same pass DID settle is the redundancy question the ledger left
#: open. On THIS deterministic path there were two gates, not three: the
#: ~2-4-session ratchet cooldown was reached only from the discretionary
#: midday `TRAIL_STOP` branch, and that day-count lock has since been
#: DELETED outright (#1810) -- no path has it now. And the two that were
#: here were NOT redundant -- all seven reconstructed refusals sat OUTSIDE
#: the then-1.25-ATR noise band (1.0 ATR since the 2026-10-09 owner
#: ruling), so the noise band would have admitted every one of them and
#: this gate was doing independent work.
#: ---------------------------------------------------------------------
#: RETIRED 2026-10-02 (owner ruling: ratchet stops on smaller moves, lock
#: gains in sooner). Everything above is HISTORY, kept because it records
#: why the 2% floor survived three earlier passes. The single cost that
#: defended it -- the cancel-then-resubmit window in which the position
#: carries no protective order -- no longer exists on the amend path, so
#: the gate has nothing left to defend and is replaced by the only floor
#: that is READ OFF THE INSTRUMENT rather than chosen.
#:
#: WHAT REPLACES IT: one venue tick. Two prices less than one tick apart
#: are not two prices -- the venue quantizes them to the same stop, so an
#: amend to a sub-tick "improvement" cannot improve protection by
#: construction. That is a property of the instrument (SEC Rule 612 /
#: Alpaca's published $0.01-at-or-above-$1, $0.0001-below split, the same
#: split `broker_parts/stop_amend_pure.py::_quantize_price` and
#: `execution/stop_records.py::_prices_match` already carry), not an
#: appetite.
#:
#: WHY NOT "no minimum at all": rejected on the instrument, not on taste.
#: With no floor at all a proposal one hundredth of a cent above the live
#: stop quantizes to the SAME stop price, so the desk would amend a
#: resting order to the price it already rests at -- a null amend that
#: spends a broker call and an owner-visible "stop moved" line for a stop
#: that did not move.
#:
#: WHY NOT a smaller percentage (0.5%, 1%): identical failure to the 2.0 it
#: would replace -- a picked constant, and one that still asks for a
#: ratchet three times larger on one name than another when expressed in
#: each name's own ATR (measured 2026-09-30, above).
#:
#: THE COSTS THE RULING ASKED ABOUT, measured in this repo rather than
#: assumed. (1) MONEY: zero -- US equity orders at this broker are
#: commission-free and an amend crosses no spread and prints no fill
#: (established above). (2) RATE LIMIT: nothing on the stop path counts or
#: budgets requests; the only 429 handling in `src/execution` is the
#: trade-stream reconnect (`broker_parts/trade_stream.py`), and the
#: deterministic trail is evaluated once per session per position, so the
#: amend count is bounded at one per position per session whatever this
#: floor is -- a floor cannot reduce a bound of one. (3) REJECTION: an
#: amend the venue cannot apply returns the amender's sentinel and the
#: caller falls back; no record in this repo measures an amend-rejection
#: rate, so none is quoted here. (4) OWNER NOISE: real, and exactly what
#: the tick floor removes -- the only amends it suppresses are the ones
#: that change no price.
MIN_RATCHET_TICKS = 1

#: Alpaca's published stock tick, the same split
#: `execution/stop_records.py` and `broker_parts/stop_amend.py` carry.
#: Duplicated rather than imported because `src.risk` does not depend on
#: `src.execution`.
_TICK_AT_OR_ABOVE_DOLLAR = 0.01
_TICK_BELOW_DOLLAR = 0.0001


def venue_tick(price: float) -> float:
    """The smallest price increment the venue accepts at this price."""
    return _TICK_AT_OR_ABOVE_DOLLAR if price >= 1.0 else _TICK_BELOW_DOLLAR


def min_ratchet_floor(stop: float, *, is_short: bool = False, min_ratchet_ticks: int = MIN_RATCHET_TICKS) -> float:
    """The nearest stop price that is a DIFFERENT stop from `stop`.

    Toward less risk only: up for a long, down for a short. A long's
    proposal must sit at or above this; a short's at or below it.
    """
    step = venue_tick(stop) * max(int(min_ratchet_ticks), 1)
    return stop - step if is_short else stop + step
