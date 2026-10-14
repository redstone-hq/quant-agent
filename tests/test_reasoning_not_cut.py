"""A stored order reason is never cut: every order builder keeps all of it.

The AI's reasoning used to be sliced to 500 characters at each order builder,
so the reason the owner read in Telegram stopped mid-sentence. Each test passes
an 800-character reason through one real builder and asserts all 800
characters reach the stored result.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.data.levels import TargetDerivation
from src.delever.trims import DeleverTrims
from src.models import Position, TargetPosition
from src.portfolio_constructor.order_build.exits import ExitOrderBuilders
from src.portfolio_constructor.order_build.long_entry import LongEntryBuilder
from src.portfolio_constructor.order_build.short_entry import ShortEntryBuilder
from src.risk.rules import apply_gross_ceiling, resolve_gross_ceiling

EQUITY = 10_000.0
REASON = ("0123456789" * 79 + "ENDMARK").ljust(800, "!")


def _position(symbol, qty, avg_entry, price):
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=price,
        market_value=qty * price,
        unrealized_pnl=qty * (price - avg_entry),
        sector="Technology",
    )


def _target(thesis_invalid_if):
    return TargetPosition(
        symbol="AAPL",
        risk_allocation_pct=2.0,
        thesis=REASON,
        thesis_invalid_if=thesis_invalid_if,
    )


def _entry_builder(cls, derived_price, stop_price):
    return cls(
        cfg=SimpleNamespace(risk_budget_pct=2.0, max_position_pct=20.0),
        derive_target=lambda symbol, analysis, entry_price, direction: TargetDerivation(
            price=derived_price, basis="structural_level", level_used=derived_price
        ),
        resolve_entry_and_stop=lambda *args, **kwargs: (100.0, stop_price),
        note_refusal=MagicMock(),
        shipped_stop_rule=lambda *args, **kwargs: "atr",
        shipped_stop_level_basis=lambda *args, **kwargs: "atr",
        target_note=lambda derivation: "",
    )


def test_sell_exit_keeps_the_whole_reason():
    decision = ExitOrderBuilders._build_sell(_target("closes below 90"), _position("AAPL", 10, 100.0, 100.0), 10.0, 0.0)
    assert decision is not None
    assert decision.reasoning.startswith(REASON)


def test_cover_exit_keeps_the_whole_reason():
    decision = ExitOrderBuilders._build_cover(
        _target("closes above 110"), _position("AAPL", -10, 100.0, 100.0), -10.0, 0.0
    )
    assert decision is not None
    assert decision.reasoning.startswith(REASON)


def test_long_entry_keeps_the_whole_reason():
    builder = _entry_builder(LongEntryBuilder, derived_price=110.0, stop_price=95.0)
    decision = builder._build_buy(_target(""), None, 0.0, 5.0, 100_000.0, 100.0)
    assert decision is not None
    assert decision.reasoning.startswith(REASON)


def test_short_entry_keeps_the_whole_reason():
    builder = _entry_builder(ShortEntryBuilder, derived_price=90.0, stop_price=105.0)
    decision = builder._build_short(_target(""), None, 0.0, -5.0, 100_000.0, 100.0)
    assert decision is not None
    assert decision.reasoning.startswith(REASON)


def test_gross_ceiling_trim_records_the_whole_reason():
    loser = _position("LOSER", 37, 120.0, 100.0)
    winner = _position("WIN", 70, 90.0, 100.0)
    ceiling = resolve_gross_ceiling(-16.0, base_x=2.0)
    outcome = apply_gross_ceiling([], [loser, winner], EQUITY, ceiling)
    assert outcome.trims and outcome.trims[0].symbol == "LOSER"
    outcome.trims[0].reasoning = REASON

    db = MagicMock()
    trims = DeleverTrims(
        alert_owner_delever_incomplete=MagicMock(),
        compute_deployable_cash=MagicMock(return_value=0.0),
        finalize_pending_protections=MagicMock(),
        format_qty=str,
        full_sell_qty=lambda q: q,
        live_delever_price=MagicMock(return_value=(None, None)),
        record_delever_shortfall=MagicMock(),
        resolve_gross_ceiling=MagicMock(),
        submit_protected_sell=MagicMock(return_value=({"id": "o1"}, {})),
        broker=MagicMock(),
        db=db,
    )
    ctx = SimpleNamespace(positions=[loser], total_value=EQUITY, run_id="t")
    trims._submit_gross_ceiling_trims(ctx, ceiling, outcome)
    assert db.insert_trade.call_args.kwargs["reasoning"] == REASON
