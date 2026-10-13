"""Persistent, cross-process circuit breaker for paid LLM analysis.

The breaker is deliberately independent from the trading decision path.  It
can suspend model requests, but it cannot stop broker reconciliation,
broker-resident protective orders, deterministic loss checks, P&L capture,
or the read-only API.

Every provider request is authorized in SQLite under ``BEGIN IMMEDIATE``.
That matters because the morning and intraday systemd jobs are separate
processes and can otherwise both observe the same remaining budget and spend
it.

=== docs/WORK.md item 14 (OWNER-APPROVED 2026-08-31, replaced 2026-09-02) ===

This module used to reserve a conservative, worst-case dollar estimate for
every call BEFORE it was made, and replace that estimate with the real cost
once the provider responded. That was deleted. Real calls settle at a
median 0.38x of the pinned worst-case rate, so the reservation held ~2.6x
what was ever really spent and stopped the desk on money that was never
spent -- three times in one hour on 2026-09-02, against ~$1/day of actual
spend on a $2.75 ceiling. The guard was causing more outages than it
prevented losses.

The replacement is exactly three things, per the owner's decision, and
nothing else:

  (a) A spend cap on the OpenRouter API key itself, OUTSIDE this codebase,
      so no bug here can defeat it. NOT IMPLEMENTED HERE -- it is not code.
      See docs/WORK.md item 14(a) for the exact provider-side limit to
      configure; do not let this fall through the cracks just because it
      has no corresponding diff.
  (b) Stop when REAL SETTLED cost actually spent today (or this session)
      hits its cap. No pre-call estimate, so nothing to be wrong about --
      `complete_call`/`fail_call` record the provider's ACTUAL returned
      cost, and `_enforce_settled_limits_locked` checks that recorded total
      against the cap, both before a call starts and immediately after one
      settles.
  (c) Stop when one session exceeds `max_calls_per_session` calls -- a
      count-based runaway-loop guard, independent of price, because a
      loop is defined by call COUNT and counting cannot be wrong about a
      rate. See `begin_call`.

The maximum overshoot of (b) is one call's real cost -- under a dollar,
measured. That is worth it against a desk switched off for a day.
"""

from __future__ import annotations

import sys as _sys
import types as _types

import logging
import json
import os
import fcntl
import random
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo
from src.cost_table import PRICING

# Every public and private name, re-exported so `src.cost_circuit.X` keeps
# resolving for every import site and every test patch target.
from src.cost_circuit.classification import (  # noqa: F401
    _DAY_QUOTA_TRIGGERS,
    _KNOWN_ZERO_COST_STATUS_CODES,
    _MID_STREAM_EXC_NAMES,
    _PRE_GENERATION_CAPACITY_STATUS_CODES,
    _PRE_SEND_TRANSPORT_EXC_NAMES,
    _PROVEN_ZERO_ROW_SQL,
    _SELF_CLEARING_HARD_TRIGGERS,
    _SESSION_QUOTA_TRIGGERS,
    _T,
    _all_attempts_provably_free,
    _cause_chain,
    _is_known_zero_cost_failure,
    _is_mid_stream_failure,
    _trigger_scope,
    _unknown_cost_row_expr,
)
from src.cost_circuit.refusal import (  # noqa: F401
    CallReservation,
    OUT_OF_CREDIT_DETAIL,
    OUT_OF_CREDIT_TRIGGER_CODE,
    OptionalPaidAnalysisRetrySkipped,
    PaidAnalysisSuspended,
    _PAYMENT_REFUSAL_STATUS_CODES,
    _fmt_settled,
    any_payment_refusal,
    is_payment_refusal,
)
from src.cost_circuit.clock import (  # noqa: F401
    _ET,
    _et_day_and_utc_bounds,
    _et_day_from_sqlite_utc,
    _legacy_mode,
)
from src.cost_circuit.schema import (  # noqa: F401
    ensure_cost_circuit_schema,
)
from src.cost_circuit.alert_outcome import (  # noqa: F401
    ALERT_STATE_SUPPRESSED,
    _alert_state_value,
    _send_alert_outcome,
)
from src.cost_circuit.alert_ledger import (  # noqa: F401
    UnavailableLLMCostCircuit,
    _durable_alert_surface_ok,
    _file_lock,
    _read_alert_outcome,
    _record_alert_attempt,
)
from src.cost_circuit.breaker import (  # noqa: F401
    LLMCostCircuitBreaker,
)
from src.cost_circuit.entrypoints import (  # noqa: F401
    activate_paid_call_session,
    protect_paid_agent,
)

logger = logging.getLogger(__name__)

_SUBMODULES = (
    "src.cost_circuit.classification",
    "src.cost_circuit.refusal",
    "src.cost_circuit.clock",
    "src.cost_circuit.schema",
    "src.cost_circuit.alert_ledger",
    "src.cost_circuit.parts.alert_formats",
    "src.cost_circuit.parts.episode_wording",
    "src.cost_circuit.parts.owner_notify",
    "src.cost_circuit.parts.circuit_state",
    "src.cost_circuit.parts.quota_holds",
    "src.cost_circuit.parts.admission",
    "src.cost_circuit.parts.settlement",
    "src.cost_circuit.parts.emergency_latch",
    "src.cost_circuit.parts.infra_retry",
    "src.cost_circuit.parts.operator_controls",
    "src.cost_circuit.parts.session_lifecycle",
    "src.cost_circuit.assembly",
    "src.cost_circuit.breaker",
    "src.cost_circuit.entrypoints",
)


# --- Patch mirroring. Every definition moved verbatim into one of the
# submodules, each of which holds its own binding of the shared names it
# imports (`_et_day_and_utc_bounds`, `_sql_stamp`, ...). Tests patch those
# names on `src.cost_circuit` and expect the moved code to see the patched
# object, so an assignment here is written through to every submodule that
# already holds the name. Undo (mock.patch / monkeypatch setting the
# original back) travels the same path. This is the ONE mirror block for
# this package; do not add a second one -- two cancel each other out.
class _CostCircuitMirroringModule(_types.ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        for module_path in _SUBMODULES:
            sub = _sys.modules.get(module_path)
            if sub is not None and name in vars(sub):
                setattr(sub, name, value)


_sys.modules[__name__].__class__ = _CostCircuitMirroringModule
