"""Owner-facing message formatting (Telegram + dashboard), split by job.

Every definition moved verbatim from the former src/trader_feed.py into the
submodules below; this package re-exports every name so `src.trader_feed.X`
and every test patch target keep working unchanged.
"""

from __future__ import annotations

import json  # noqa: F401
import sys as _sys
import types as _types
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from src.notifier import (
    _actionable_coverage_gaps,
    _attr_or_key,
    _clip_text,
    _DB_PATH as _NOTIFIER_DB_PATH,
    _fmt_signed_money,
    _lookup_company_profiles,
    _margin_interest_lines,
    _new_block,
    _new_section,
    _seal_section,
    company_name,
    describe_ai_cost,
    describe_data_status,
    describe_skipped_decision,
    fmt_time_12h,
    humanize_status,
    format_session_result as _base_format_session_result,
    TelegramNotifier,
)
from src.trading_calendar import SESSION_WINDOWS, et_now, in_session_window, to_et
from src.trader_feed.common import (  # noqa: F401
    _BASE_ONLY_STATUSES,
    _COMPANY_NAME_CAP,
    _DB_PATH,
    _DELIBERATE_SKIP_REASONS,
    _DETAILS_SAFETY_RESERVE_CHARS,
    _FILL_STATE_WORDS,
    _INTRADAY_SILENT_STATUSES,
    _ORDER_END_WORDS,
    _PNL_UNAVAILABLE_FALLBACK,
    _PNL_UNAVAILABLE_REASONS,
    _SKIP_WHO_LABELS,
    _SWEEP_SYMBOLS,
    _all_symbols,
    _b,
    _blocked_rows,
    _budgeted_sections,
    _classify_trades,
    _clip,
    _decision_action_for,
    _done_rows,
    _empty_snapshot,
    _execution_rows,
    _fault_count,
    _fill_state_plain,
    _fmt_elapsed,
    _fmt_pnl_line,
    _looked_at_block,
    _looked_at_groups,
    _looked_at_rows,
    _machine_detail,
    _number,
    _order_end_plain,
    _outcome_word,
    _pm_pass_reason,
    _pnl_section_lines,
    _pnl_unavailable_sentence,
    _profiles,
    _read_run,
    _signal_rows,
    _skip_is_fault,
    _skip_who,
    _status_emoji,
    _symbol_stop,
    _ticker_co,
    _trade_reached_broker,
    _traded_word,
    _wrap_details,
    logger,
)
from src.trader_feed.decision import (  # noqa: F401
    _RISK_CATEGORY_WORDS,
    _append_blocked,
    _append_book,
    _append_coverage_gaps,
    _append_done,
    _append_footer,
    _append_gate_and_execution,
    _append_held,
    _append_intraday_evidence_freshness,
    _append_market,
    _append_no_trade_reason,
    _append_pm,
    _append_risk,
    _append_rotation,
    _append_signals,
    _append_watch,
    _format_decision_session,
    _held_symbols,
    _risk_category_words,
    _signal_row_line,
    _watch_rows,
    extract_alert_symbols,
    format_coverage_gap_line,
)
from src.trader_feed.naked import (  # noqa: F401
    naked_position_alert,
    protection_undetermined_alert,
    send_naked_position_alert,
    stop_coverage_was_audited,
    uncovered_stop_gaps,
)
from src.trader_feed.evening import (  # noqa: F401
    _FORM_WORDS,
    _RISK_SCALE,
    _append_evening_banners,
    _append_evening_positions,
    _append_evening_tomorrow,
    _append_evening_watchlist,
    _earnings_filing_line,
    _evening_cost_line,
    _evening_fractional_line,
    _evening_meta_line,
    _evening_outcome,
    _evening_overnight_remainder_detail,
    _evening_pnl_lines,
    _form_words,
    _format_earnings,
    _format_evening,
    _format_position_review,
    _risk_with_scale,
)
from src.trader_feed.intraday import (  # noqa: F401
    _HOUR_WINDOW_MINUTES,
    _INTRA_CHECK_TIMER_PATH,
    _ONCALENDAR_PER_HOUR_RE,
    _ON_DEMAND_COVERAGE_TEXT,
    _ON_DEMAND_PERIOD_LINE,
    _ON_DEMAND_SCANNED_TEXT,
    _format_hourly_desk_check,
    _format_intra_check,
    _format_intraday,
    _hourly_checkpoint_minute,
    _intra_check_tick_minutes,
    _intraday_tick_actionable,
    _is_hourly_checkpoint,
    _is_midday_collision_tick,
    _midday_collision_tick_minute,
    _movers_scanned_text,
    _position_rows_from_broker,
    _read_hour_evidence,
    _read_hour_trade_count,
    _read_hour_trades,
)
from src.trader_feed.dispatch import (  # noqa: F401
    format_session_result,
)
from src.trader_feed.stored import (  # noqa: F401
    _STORED_EVENING_GAP_WORDS,
    _STORED_SESSION_GAP_WORDS,
    _STORED_SESSION_LABELS,
    format_desk_status,
    read_stored_evening,
    read_stored_intra_check,
    read_stored_session_report,
    render_stored_evening,
    render_stored_intra_check,
    render_stored_session_report,
)

_SUBMODULES = tuple(f"src.trader_feed.{m}" for m in ("common", "decision", "evening", "intraday", "dispatch", "stored"))

# --- Patch mirroring. Tests patch names on `src.trader_feed` (`_DB_PATH`,
# `et_now`, `_read_run`, `_lookup_company_profiles`, ...) and expect the moved
# code to see the patched object. Mirroring the assignment into any submodule
# that already holds the name keeps it the SAME object on both sides; a
# delete (how `mock.patch` unwinds) puts the pristine object back. Same
# design as src/notifier/__init__.py. This is the ONE mirror block for this
# package; do not add a second one.
_PRISTINE: dict[str, object] = {}


class _TraderFeedMirroringModule(_types.ModuleType):
    def __setattr__(self, name: str, value) -> None:
        if name in vars(self):
            _PRISTINE.setdefault(name, vars(self)[name])
        super().__setattr__(name, value)
        for module_path in _SUBMODULES:
            sub = _sys.modules.get(module_path)
            if sub is not None and name in vars(sub):
                setattr(sub, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        pristine = _PRISTINE.pop(name, None)
        for module_path in _SUBMODULES:
            sub = _sys.modules.get(module_path)
            if sub is not None and name in vars(sub):
                if pristine is None:
                    delattr(sub, name)
                else:
                    setattr(sub, name, pristine)


_sys.modules[__name__].__class__ = _TraderFeedMirroringModule
