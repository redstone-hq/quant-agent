"""Make a backtest sweep PROVE it reached the number it claims to vary.

WHY THIS EXISTS. Four separate times this project read a byte-identical
A/B result as "the parameter is inert" when the truth was "the harness
never reached the parameter". At least one of those values moved real
money materially once it was genuinely wired. A sweep that cannot show a
read count is a trap, because the two outcomes — inert value and
unreached value — are indistinguishable in the numbers.

So: every value this engine lets a sweep vary is read through a
`SweepMeter`, and every read is COUNTED at the site that actually uses
it. A run that reports ``0 reads`` for a value has not measured that
value; it has failed to reach it, and says so in its own output.

ONE METER PER RUN, NO STORED BOOKKEEPING. The meter hangs off
`BacktestParams`, which is already built once per run and threaded
through the engine, so a count is born and dies with the run it
describes. There is no module-level accumulator here: nothing survives
the run, so nothing can leak into the next one or be reset dishonestly.

THE TWO WAYS A VALUE GOES UNREAD, both found in this codebase:

1. ``from X import NAME`` binds the VALUE into the importing module's
   namespace at import time. Reassigning ``X.NAME`` afterwards changes
   nothing the importer sees. ``compute_trailing_stop`` (which lives in
   ``src/risk/trail_evaluate.py``, not in ``src/risk/trailing.py`` where
   its constants are defined) reads its chandelier and noise-band
   multiples this way.

2. ``def f(..., knob=MODULE_CONSTANT)`` evaluates the default ONCE, at
   function-definition time. ``find_structural_levels`` freezes its
   ``tolerance_pct`` / ``min_touches`` this way, so a
   sweep that reassigns the module constant is swept past silently.

The repairs are ENGINE-SIDE ONLY and change no value and no trading
rule. For (2) the engine passes the knobs EXPLICITLY on every call, read
late from the defining module, so a reassignment is honoured and
counted. For (1) `SweepMeter.counting()` installs a counting wrapper of
the SAME numeric value into the namespace that actually reads it, for
the duration of one run only, and restores the original afterwards.

NOTHING HERE RAISES. A diagnostic that halts a backtest is worse than
the problem it reports. A zero count is reported, never enforced.
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

#: Every value a sweep may vary through this engine, mapped to a one-line
#: description used in the run's own output. A name absent from here is
#: still counted if read, but an unknown name in an override is reported
#: rather than silently applied to nothing.
KNOWN_VALUES: dict[str, str] = {
    "levels.cluster_tolerance_pct": "level cluster width pct (src/data/levels.py)",
    "levels.min_touches": "touches before a level counts (src/data/levels.py)",
    "trailing.chandelier_atr_multiple": "chandelier ATR multiple (src/risk/trailing.py)",
    "trailing.noise_band_atr_multiple": "trail noise-band ATR multiple (src/risk/trailing.py)",
}

#: (module that READS the constant, attribute name there, swept name).
#: The reading module is the one that matters: patching the module that
#: DEFINES the constant is exactly the mistake this file exists to make
#: visible, because `from X import NAME` already copied the value out.
FROZEN_SITES: tuple[tuple[str, str, str], ...] = (
    ("src.risk.trail_evaluate", "CHANDELIER_ATR_MULTIPLE", "trailing.chandelier_atr_multiple"),
    ("src.risk.trail_evaluate", "NOISE_BAND_ATR_MULTIPLE", "trailing.noise_band_atr_multiple"),
)


class CountedFloat(float):
    """A float that is numerically its own value and counts its uses.

    Installed in place of a module constant that another module froze
    into its own namespace with ``from X import NAME``. Every arithmetic
    use bumps the owning run's meter and returns a PLAIN float, so
    nothing downstream can observe this type or behave differently
    because of it.
    """

    __slots__ = ("_meter", "_swept_name")

    def __new__(cls, value: float, meter: "SweepMeter", name: str) -> "CountedFloat":
        obj = super().__new__(cls, value)
        obj._meter = meter
        obj._swept_name = name
        return obj

    def _used(self) -> float:
        self._meter.bump(self._swept_name)
        return float(self)

    def __mul__(self, other: Any) -> Any:
        return self._used() * other

    def __rmul__(self, other: Any) -> Any:
        return other * self._used()

    def __add__(self, other: Any) -> Any:
        return self._used() + other

    def __radd__(self, other: Any) -> Any:
        return other + self._used()

    def __sub__(self, other: Any) -> Any:
        return self._used() - other

    def __rsub__(self, other: Any) -> Any:
        return other - self._used()

    def __truediv__(self, other: Any) -> Any:
        return self._used() / other

    def __rtruediv__(self, other: Any) -> Any:
        return other / self._used()

    def __neg__(self) -> Any:
        return -self._used()

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"CountedFloat({float(self)!r}, {self._swept_name!r})"


@dataclass
class SweepMeter:
    """One run's swept-value overrides and read counts.

    Built per `BacktestParams`, which is built per run — so this object
    is not stored bookkeeping: it describes exactly one run and is
    discarded with it.
    """

    #: Values this run is varying, by swept name.
    overrides: dict[str, Any] = field(default_factory=dict)
    #: Times each name was read at its use site DURING THIS RUN.
    reads: dict[str, int] = field(default_factory=dict)

    # -- counting ---------------------------------------------------------

    def bump(self, name: str) -> None:
        self.reads[name] = self.reads.get(name, 0) + 1

    def read(self, name: str, default: Any) -> Any:
        """Return the value for `name`, counting this as one read.

        `default` is looked up by the CALLER, late, from the module that
        defines the constant — so reassigning that module attribute is
        honoured even though the consuming function froze its own
        default at definition time.
        """
        self.bump(name)
        return self.overrides.get(name, default)

    def counts(self) -> dict[str, int]:
        """Read counts for this run: every known value, plus anything
        else read. A value present with 0 was NOT reached."""
        out = {name: 0 for name in KNOWN_VALUES}
        out.update(self.reads)
        return out

    # -- overrides --------------------------------------------------------

    def set_override(self, name: str, value: Any) -> None:
        """Set a value this run is varying. An unknown name is accepted
        and named in the output rather than refused: this is a
        diagnostic, and a diagnostic does not refuse."""
        self.overrides[name] = value

    def unknown_overrides(self) -> list[str]:
        """Override names this engine has no reading site for. A sweep
        over one of these can only ever report zero reads."""
        return sorted(n for n in self.overrides if n not in KNOWN_VALUES)

    # -- the constants another module froze into its own namespace --------

    @contextmanager
    def counting(self) -> Iterator["SweepMeter"]:
        """For the duration of one run, replace each frozen constant with
        a value-identical counting wrapper in the namespace that READS
        it, then restore the original exactly.

        Restoring is what keeps this per-run: no wrapper, and no count,
        outlives the `with` block. A site that cannot be imported is
        skipped — a diagnostic must never break a run.
        """
        restore: list[tuple[Any, str, Any]] = []
        for reader, attr, name in FROZEN_SITES:
            try:
                module = importlib.import_module(reader)
                current = getattr(module, attr)
            except Exception:  # pragma: no cover - diagnostic, never fatal
                continue
            if not isinstance(current, float):
                continue
            if isinstance(current, CountedFloat):
                continue
            restore.append((module, attr, current))
            value = self.overrides.get(name, current)
            setattr(module, attr, CountedFloat(value, self, name))
        try:
            yield self
        finally:
            for module, attr, original in restore:
                setattr(module, attr, original)

    # -- reporting --------------------------------------------------------

    def format_read_counts(self, label: str = "") -> str:
        """The block a run prints so a zero-read sweep is self-evident.

        A sweep reporting identical numbers AND a zero read count has not
        measured anything; that is an honest failure. Identical numbers
        with a non-zero read count is a real result.
        """
        seen = self.counts()
        head = f"SWEPT-VALUE READS{(' — ' + label) if label else ''}"
        lines = [
            "=" * 74,
            head,
            "=" * 74,
            "Times this run actually READ each value a sweep can vary.",
            "0 means the run never reached it: any A/B over it is a NON-RESULT,",
            "however different or identical the numbers look.",
            "",
        ]
        width = max(len(n) for n in seen)
        for name in sorted(seen):
            n = seen[name]
            swept = " [SWEPT]" if name in self.overrides else ""
            flag = "" if n else "   <-- NEVER READ"
            lines.append(f"  {name.ljust(width)}  {n:>8,} reads{swept}{flag}")
        unknown = self.unknown_overrides()
        if unknown:
            lines.append("")
            lines.append("  Overrides with no reading site in this engine (can only ever report 0):")
            for name in unknown:
                lines.append(f"    {name}")
        swept_unread = sorted(n for n in self.overrides if seen.get(n, 0) == 0)
        lines.append("")
        if swept_unread:
            lines.append(
                "  VERDICT: this sweep did NOT reach " + ", ".join(swept_unread) + " — its result is a non-result."
            )
        elif self.overrides:
            lines.append("  VERDICT: every swept value was read at its use site.")
        else:
            lines.append("  VERDICT: no override set — this is a baseline run.")
        return "\n".join(lines)
