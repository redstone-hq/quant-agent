"""Trend-scaled structural-break confirmation (owner mandate 2026-09-24).

Two sourced regimes (see the module note in `src/risk/exit_guard.py`):

  STRONG-WITH-TREND — a break WITH a strong uptrend (ADX>=25 AND price above a
    rising 200MA AND 50>200; short mirror): the two-close floor AND the prior
    structural swing-low must ALSO close broken (most patient — likely shakeout).
  DEFAULT — everything else (against/weak/tangled, or no trend data): the
    ratified two-consecutive-close floor, reclaim resets. Nothing exits faster
    than this floor — there is NO single-close path.

The break MARGIN is always BREAK_CONFIRMATION_ATR_MULTIPLE (1.0 ATR). These tests also
cover the cross-day counting fixes (streak adjacency, margin consistency) and
the owner-facing voicing to both surfaces, including the silent-action guard
when the Telegram send RETURNS FALSE (it does not raise).

With atr=1.0 and a level at 90 the long break margin is 1.0, so a long support
breaks at a close <= 89.0.
"""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import MagicMock, patch

from src.data.levels import CLUSTER_TOLERANCE_PCT
from src.data.technical import ADX_PERIOD, compute_indicators
from src.models import OHLCV
from src.risk.exit_guard import (
    ADX_STRONG_TREND_THRESHOLD,
    BREAK_CONFIRMATION_ATR_MULTIPLE,
    TREND_CONFIRMING_CLOSES,
    REGIME_STRONG_WITH_TREND,
    REGIME_DEFAULT,
    check_structural_protection,
    classify_trend_context,
    render_owner_break_message,
)
from tests.pipeline_factory import build_pipeline

MIN_TOUCHES = 5
_LEVELS = [90.0]
_TOUCHES = {90.0: 6}

# A rising 200-MA well below price, with 50>200 -> a genuine uptrend a dip
# through the 90 support sits inside (a shakeout candidate).
_UPTREND = dict(ma_50=100.0, ma_200=80.0, ma_200_prior=79.0)
# 50<200 and price below the 200-MA -> against/dead trend (DEFAULT regime).
_DOWNTREND = dict(ma_50=95.0, ma_200=100.0, ma_200_prior=100.0)

_SESSIONS = ["2026-03-09", "2026-03-06", "2026-03-05", "2026-03-04"]


def _common(**over):
    base = dict(
        thesis_invalid_if=None,
        entry_price=100.0,
        stop_loss=90.3,
        atr=1.0,
        is_short=False,
        computed_levels=list(_LEVELS),
        computed_level_touches=dict(_TOUCHES),
        # Item 215 (2026-09-30): a level backs the stop only when the stop
        # rests inside the traded range of a bar that DREW it. The 90.30
        # stop sits on the session that turned at 90.00 and traded to 90.60;
        # every level states the narrowest bar that can have drawn it.
        computed_level_bars={lv: [(lv, lv)] for lv in _LEVELS} | {90.0: [(90.0, 90.6)]},
        min_level_touches=MIN_TOUCHES,
        level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
    )
    base.update(over)
    return base


def _rec(bar_date, close, broken=True):
    return {"bar_date": bar_date, "raw_broken": broken, "close": close}


# --------------------------------------------------------------------------
# ADX / 200-MA slope are computed from the bars.
# --------------------------------------------------------------------------


def _trend_bars(n: int, step: float, *, start: float = 50.0) -> list[OHLCV]:
    bars = []
    price = start
    for i in range(n):
        prev = price
        price = max(1.0, price + step)
        bars.append(
            OHLCV(
                symbol="TST",
                date=dt.date(2026, 1, 1) + dt.timedelta(days=i),
                open=prev,
                high=max(prev, price) * 1.01,
                low=min(prev, price) * 0.99,
                close=price,
                volume=1000,
            )
        )
    return bars


def test_adx_is_computed_and_matches_direct_ta():
    import pandas as pd
    import ta

    bars = _trend_bars(60, step=0.5)
    ind = compute_indicators("TST", bars)
    assert ind.adx_14 is not None
    assert ind.adx_14 >= ADX_STRONG_TREND_THRESHOLD
    df = pd.DataFrame([b.model_dump() for b in bars]).set_index("date").sort_index()
    direct = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=ADX_PERIOD)
    assert ind.adx_14 == round(float(direct.adx().iloc[-1]), 2)


def test_adx_none_when_too_few_bars():
    ind = compute_indicators("TST", _trend_bars(2 * ADX_PERIOD - 1, step=0.5))
    assert ind.adx_14 is None


def test_ma_200_slope_is_exposed():
    ind = compute_indicators("TST", _trend_bars(220, step=0.3))
    assert ind.ma_200 is not None and ind.ma_200_prior is not None
    assert ind.ma_200 > ind.ma_200_prior  # a rising 200-MA in an uptrend


# --------------------------------------------------------------------------
# classify_trend_context -> two sourced regimes.
# --------------------------------------------------------------------------


def test_classify_regimes_long():
    # Strong with-trend: uptrend structure + ADX>=25.
    assert (
        classify_trend_context(
            is_short=False,
            current_price=88.5,
            adx=30.0,
            **_UPTREND,
        )
        == REGIME_STRONG_WITH_TREND
    )
    # Against/dead trend -> default (no fast path).
    assert (
        classify_trend_context(
            is_short=False,
            current_price=88.5,
            adx=30.0,
            **_DOWNTREND,
        )
        == REGIME_DEFAULT
    )
    # With uptrend structure but ADX < 25 -> default (no moderate regime).
    assert (
        classify_trend_context(
            is_short=False,
            current_price=88.5,
            adx=22.0,
            **_UPTREND,
        )
        == REGIME_DEFAULT
    )
    # No MA data -> default.
    assert (
        classify_trend_context(
            is_short=False,
            current_price=88.5,
            ma_50=None,
            ma_200=None,
            adx=30.0,
        )
        == REGIME_DEFAULT
    )
    # No ADX -> default.
    assert (
        classify_trend_context(
            is_short=False,
            current_price=88.5,
            **_UPTREND,
        )
        == REGIME_DEFAULT
    )
    # Flat 200-MA (no slope) is not "with the trend".
    assert (
        classify_trend_context(
            is_short=False,
            current_price=88.5,
            adx=30.0,
            ma_50=100.0,
            ma_200=80.0,
            ma_200_prior=80.0,
        )
        == REGIME_DEFAULT
    )


def test_classify_short_mirrors_long():
    # Short with-trend strong: price below a FALLING 200MA (far above the
    # popped resistance) and 50<200.
    assert (
        classify_trend_context(
            is_short=True,
            current_price=112.0,
            adx=30.0,
            ma_50=110.0,
            ma_200=130.0,
            ma_200_prior=131.0,
        )
        == REGIME_STRONG_WITH_TREND
    )
    # Rising 200MA / 50>200 against the short -> default.
    assert (
        classify_trend_context(
            is_short=True,
            current_price=112.0,
            adx=30.0,
            ma_50=140.0,
            ma_200=130.0,
            ma_200_prior=129.0,
        )
        == REGIME_DEFAULT
    )


# --------------------------------------------------------------------------
# DEFAULT regime: two consecutive closes (fast floor); NO single-close exit.
# --------------------------------------------------------------------------


def test_default_break_needs_two_consecutive_closes_not_one():
    kw = dict(current_price=88.5, adx=30.0, **_DOWNTREND)  # against -> default
    confirmed = check_structural_protection(
        **_common(**kw),
        prior_break_records=[_rec("2026-03-09", 88.5)],
        prior_session_dates=_SESSIONS,
    )
    assert confirmed.trend_context == REGIME_DEFAULT
    assert confirmed.confirming_closes_needed == TREND_CONFIRMING_CLOSES == 2
    assert confirmed.protected is False
    assert confirmed.basis == "structural_level_broken"
    # A single close never lifts protection (no fast one-close path).
    one_close = check_structural_protection(
        **_common(**kw),
        prior_break_records=[],
        prior_session_dates=_SESSIONS,
    )
    assert one_close.protected is True
    assert one_close.basis == "structural_level_pending_confirmation"
    assert (one_close.confirming_closes_seen, one_close.confirming_closes_needed) == (1, 2)


def test_default_reclaim_resets():
    r = check_structural_protection(
        **_common(current_price=88.5, adx=30.0, **_DOWNTREND),
        prior_break_records=[_rec("2026-03-09", 91.0, broken=False)],
        prior_session_dates=_SESSIONS,
    )
    assert r.protected is True
    assert r.basis == "structural_level_pending_confirmation"


# --------------------------------------------------------------------------
# STRONG-WITH-TREND: two closes AND the prior swing-low must also break.
# --------------------------------------------------------------------------


def test_strong_with_trend_holds_until_prior_low_breaks():
    levels = [85.0, 90.0]
    touches = {90.0: 6, 85.0: 6}
    two_closes_only = check_structural_protection(
        **_common(
            current_price=88.5,
            adx=30.0,
            computed_levels=levels,
            computed_level_touches=touches,
            computed_level_bars={90.0: [(90.0, 90.6)], 85.0: [(85.0, 85.0)]},
            **_UPTREND,
        ),
        prior_break_records=[_rec("2026-03-09", 88.5)],
        prior_session_dates=_SESSIONS,
    )
    assert two_closes_only.trend_context == REGIME_STRONG_WITH_TREND
    assert two_closes_only.protected is True  # 85 prior low not yet broken
    assert two_closes_only.basis == "structural_level_pending_confirmation"

    prior_low_broken = check_structural_protection(
        **_common(
            current_price=83.5,
            adx=30.0,
            computed_levels=levels,
            computed_level_touches=touches,
            computed_level_bars={90.0: [(90.0, 90.6)], 85.0: [(85.0, 85.0)]},
            **_UPTREND,
        ),
        prior_break_records=[_rec("2026-03-09", 88.5)],
        prior_session_dates=_SESSIONS,
    )
    assert prior_low_broken.protected is False
    assert prior_low_broken.basis == "structural_level_broken"


def test_strong_with_trend_falls_back_to_two_closes_without_prior_low():
    r = check_structural_protection(
        **_common(current_price=88.5, adx=30.0, **_UPTREND),
        prior_break_records=[_rec("2026-03-09", 88.5)],
        prior_session_dates=_SESSIONS,
    )
    assert r.trend_context == REGIME_STRONG_WITH_TREND
    assert r.protected is False  # no lower level -> two closes suffice


# --------------------------------------------------------------------------
# Cross-day counting fixes: gap resets, margin consistency.
# --------------------------------------------------------------------------


def test_gap_resets_the_streak():
    r = check_structural_protection(
        **_common(current_price=88.5, adx=30.0, **_DOWNTREND),
        prior_break_records=[_rec("2026-03-06", 88.5)],  # gap: not 2026-03-09
        prior_session_dates=_SESSIONS,
    )
    assert r.protected is True
    assert r.basis == "structural_level_pending_confirmation"
    assert r.confirming_closes_seen == 1


def test_margin_conflation_cannot_confirm_early():
    stale = check_structural_protection(
        **_common(current_price=88.5, adx=30.0, **_DOWNTREND),
        prior_break_records=[_rec("2026-03-09", 89.2)],  # didn't clear 1.0 margin
        prior_session_dates=_SESSIONS,
    )
    assert stale.protected is True
    good = check_structural_protection(
        **_common(current_price=88.5, adx=30.0, **_DOWNTREND),
        prior_break_records=[_rec("2026-03-09", 88.5)],
        prior_session_dates=_SESSIONS,
    )
    assert good.protected is False


# --------------------------------------------------------------------------
# The margin is always 1.0 ATR across regimes.
# --------------------------------------------------------------------------


def test_margin_is_always_one_atr_across_regimes():
    assert BREAK_CONFIRMATION_ATR_MULTIPLE == 1.0
    for kw in (dict(adx=30.0, **_DOWNTREND), dict(adx=30.0, **_UPTREND)):
        broke = check_structural_protection(**_common(current_price=88.7, **kw))
        assert broke.raw_broken is True  # 88.7 beyond 1.0-ATR margin in both
        intact = check_structural_protection(**_common(current_price=89.5, **kw))
        assert intact.raw_broken is False  # 89.5 within 1.0 ATR in both
        assert intact.basis == "structural_level_intact"


# --------------------------------------------------------------------------
# Voicing the why to BOTH surfaces, truthful wording.
# --------------------------------------------------------------------------


def _exit_check():
    return check_structural_protection(
        **_common(current_price=88.5, adx=30.0, **_DOWNTREND),
        prior_break_records=[_rec("2026-03-09", 88.5)],
        prior_session_dates=_SESSIONS,
    )


def test_owner_message_wording_lifted_and_kept():
    msg = render_owner_break_message("NVDA", _exit_check())
    assert msg and "NVDA" in msg
    assert "hold-protection lifted" in msg.lower()
    assert "real breakdown" in msg.lower()
    assert "sold" not in msg.lower()  # never claims a sale
    assert "90 support" in msg

    hold_check = check_structural_protection(
        **_common(
            current_price=88.5,
            adx=30.0,
            computed_levels=[85.0, 90.0],
            computed_level_touches={90.0: 6, 85.0: 6},
            computed_level_bars={90.0: [(90.0, 90.6)], 85.0: [(85.0, 85.0)]},
            **_UPTREND,
        ),
        prior_break_records=[_rec("2026-03-09", 88.5)],
        prior_session_dates=_SESSIONS,
    )
    hmsg = render_owner_break_message("AAPL", hold_check)
    assert hmsg and "AAPL" in hmsg
    assert "hold-protection kept" in hmsg.lower()
    assert "shakeout" in hmsg.lower()
    assert "uptrend" in hmsg.lower()
    assert "sold" not in hmsg.lower()


def test_no_owner_message_on_intact_basis():
    intact = check_structural_protection(
        **_common(current_price=95.0, adx=30.0, **_UPTREND),
    )
    assert intact.owner_reason == ""
    assert render_owner_break_message("NVDA", intact) is None


def _voicing_pipeline():

    p = build_pipeline(db=MagicMock())
    return p


def _board_rows(pipeline):
    rows = []
    for call in pipeline.db.insert_specialist_evidence.call_args_list:
        kw = call.kwargs
        if kw.get("kind") != "structural_break_trend_context":
            continue
        rows.append((kw.get("symbol"), json.loads(kw["evidence_json"])))
    return rows


def test_voicing_is_recorded_for_the_session_message_and_not_paged():
    p = _voicing_pipeline()
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        p._voice_structural_protection_break(
            symbol="NVDA",
            run_id="run-1",
            check=_exit_check(),
        )
    # Owner 2026-10-14: the verdict is recorded for the session message's one
    # line per stock, not paged on its own (tests/test_one_message_per_stock.py).
    assert alert.call_count == 0
    rows = _board_rows(p)
    assert len(rows) == 1
    sym, payload = rows[0]
    assert sym == "NVDA"
    owner_text = payload["owner_reason"]
    assert "NVDA" in owner_text and "hold-protection lifted" in owner_text.lower()
    assert payload["protected"] is False
    assert payload["trend_context"] == REGIME_DEFAULT


def test_voicing_deduplicated_within_a_run():
    p = _voicing_pipeline()
    check = _exit_check()
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=check)
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=check)
    assert alert.call_count == 0
    assert len(_board_rows(p)) == 1


def test_voicing_survives_a_telegram_failure_and_keeps_board():
    p = _voicing_pipeline()
    with patch("src.notifier.send_owner_alert", side_effect=RuntimeError("down")):
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=_exit_check())
    assert len(_board_rows(p)) == 1  # board landed -> a surface reached
    assert ("run-1", "NVDA", "structural_level_broken") in p._voiced_structural_breaks


# --------------------------------------------------------------------------
# FIX 1: send_owner_alert RETURNS False (does not raise). If the board write
# ALSO fails, the dedup slot must NOT be consumed, so a later cycle retries.
# --------------------------------------------------------------------------


def test_both_surface_failures_do_not_consume_dedup_slot():
    p = _voicing_pipeline()
    p.db.insert_specialist_evidence.side_effect = RuntimeError("board down")
    check = _exit_check()
    # Telegram returns False (the real failure mode), board raises.
    with patch("src.notifier.send_owner_alert", return_value=False):
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=check)
    assert getattr(p, "_voiced_structural_breaks", set()) == set()

    # A later cycle whose board write now succeeds voices it (not suppressed).
    p.db.insert_specialist_evidence.side_effect = None
    with patch("src.notifier.send_owner_alert", return_value=False):
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=check)
    assert p.db.insert_specialist_evidence.call_count == 2  # retried
    assert ("run-1", "NVDA", "structural_level_broken") in p._voiced_structural_breaks


def test_telegram_false_alone_still_consumes_when_board_succeeds():
    # Board OK + Telegram returns False -> a surface WAS reached -> consumed.
    p = _voicing_pipeline()
    with patch("src.notifier.send_owner_alert", return_value=False):
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=_exit_check())
    assert len(_board_rows(p)) == 1
    assert ("run-1", "NVDA", "structural_level_broken") in p._voiced_structural_breaks
