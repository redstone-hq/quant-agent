"""`build_risk_gate` — a `RiskGate` from values, not a pipeline.

Conversion step 7, finished. The old `RiskGateMixin`
(`src/pipeline_risk_gate_mixin.py`, deleted 2026-10-05) forwarded seven names
off `TradingPipeline` into a `RiskGate` it resolved per call from `self`. That
is the cosmetic split: the gate could not be had without a pipeline in hand.
This builder takes the four collaborators BY VALUE, so the gate can be
constructed, driven and tested with no pipeline anywhere — see
`tests/test_boundary_risk_gate.py`, which proves in a subprocess that building
it never imports `src.pipeline`.

`sweeper` is a zero-argument callable returning the cash sweeper or None; a
caller with none gets a gate whose sweep hooks are structural no-ops.
"""

from __future__ import annotations

from collections.abc import Callable

from src.pipeline_risk_gate import RiskGate


def _no_sweeper() -> None:
    return None


def build_risk_gate(
    *,
    config,
    risk_engine=None,
    db=None,
    sweeper: Callable[[], object | None] | None = None,
    atr_for_symbol: Callable[[str], float | None] | None = None,
) -> RiskGate:
    """The risk-verdict gate, from plain collaborators."""
    return RiskGate(
        risk_engine=risk_engine,
        db=db,
        sweeper=sweeper if sweeper is not None else _no_sweeper,
        config=config,
        atr_for_symbol=atr_for_symbol,
    )


class RiskGateSlot:
    """Descriptor giving an object a `risk_gate`, rebuilt from its live values.

    Tests build the pipeline via `__new__` (see tests/test_boundary_risk_gate.py)
    and assign `risk_engine`, `db`,
    `config` or `_sweeper` afterwards, so a stored gate is reused only while it
    holds the very collaborators the owner holds now; otherwise a fresh one is
    built from them and stored. Assignable. It lives here, not on the pipeline,
    so the pipeline gains one line rather than a method.
    """

    def __set_name__(self, owner, name) -> None:
        self._key = "_" + name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        risk_engine = getattr(obj, "risk_engine", None)
        db = getattr(obj, "db", None)
        config = getattr(obj, "config", None)
        sweeper = obj._sweeper
        atr_for_symbol = getattr(obj, "_atr_for_symbol", None)
        gate = obj.__dict__.get(self._key)
        if (
            isinstance(gate, RiskGate)
            and gate.risk_engine is risk_engine
            and gate.db is db
            and gate.config is config
            and gate._sweeper == sweeper
            and gate._atr_for_symbol == atr_for_symbol
        ):
            return gate
        gate = build_risk_gate(
            config=config, risk_engine=risk_engine, db=db, sweeper=sweeper, atr_for_symbol=atr_for_symbol
        )
        obj.__dict__[self._key] = gate
        return gate

    def __set__(self, obj, gate) -> None:
        obj.__dict__[self._key] = gate
