"""The screen refuses a name that did not trade on every session of its year."""

from __future__ import annotations

from dataclasses import replace

from src import universe_screen as us
from tests.test_universe_screen import TH, _good_bars


def _failures(bars, ref=None):
    return us.check_bars(bars, TH, ref)[0]


def _set(bars, i, **kw):
    out = list(bars)
    out[i] = replace(out[i], **kw) if hasattr(out[i], "__dataclass_fields__") else out[i].model_copy(update=kw)
    return out


def test_fully_traded_name_passes():
    bars = _good_bars()
    assert _failures(bars, {b.date for b in bars}) == []


def test_one_zero_volume_day_is_refused():
    bars = _set(_good_bars(), -50, volume=0)
    assert "not_traded_every_session" in _failures(bars)


def test_one_flat_day_is_refused():
    bars = _good_bars()
    flat = bars[-30].close
    bars = _set(bars, -30, high=flat, low=flat)
    assert "not_traded_every_session" in _failures(bars)


def test_one_missing_session_is_refused():
    full = _good_bars()
    ref = {b.date for b in full}
    bars = full[:-40] + full[-39:]
    assert "not_traded_every_session" in _failures(bars, ref)


def test_reason_has_plain_words():
    assert us.plain_reasons(["not_traded_every_session"])
