"""Desk archetype step B — ADMISSION (owner ruling 19 Oct 2026, docs/OUTCOME.md
"The desk archetype").

A name is admitted when the chart seat confirms the trade direction AND at
least one name-level seat (news, earnings, smart money) took a supported side
in that direction. An opposed name-level seat no longer blocks: it becomes an
objection (seat, direction, reason) the planning AI must answer, and a buy
over an objection proceeds only with a written "overrule" on the target.
Macro never counts as a supporter (it tilts size only). Shorts mirror longs.

Every test here fails on the pre-archetype trunk: imports of the new names
sit inside each test so each case fails for its own reason.
"""

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.risk.rules import own_bar_block_reason
from tests.test_conflict_adjudication import _buy_target, _decision
from tests.test_conviction_bar import _macro, _v

_NAME_OPPOSED = [_v("technical", "NVDA"), _v("news", "NVDA"), _v("earnings", "NVDA", direction="bearish")]


def test_opposed_name_level_seat_admits_with_an_objection():
    from src.risk.conviction_bar import seat_objections

    assert own_bar_block_reason(_NAME_OPPOSED, direction="bullish") is None
    objections = seat_objections(_NAME_OPPOSED, direction="bullish")
    assert objections == [{"seat": "earnings", "direction": "bearish", "reason": "a checkable observed fact"}]


def test_macro_is_never_the_supporter():
    reason = own_bar_block_reason(
        [_v("technical", "XOM"), _macro("XOM", direction="bullish", sector_specific=True)], direction="bullish"
    )
    assert reason is not None and "no name-level seat" in reason


def test_macro_against_neither_blocks_nor_objects():
    from src.risk.conviction_bar import seat_objections

    verdicts = [_v("technical", "XOM"), _v("news", "XOM"), _macro("XOM", sector_specific=True)]
    assert own_bar_block_reason(verdicts, direction="bullish") is None
    assert seat_objections(verdicts, direction="bullish") == []


def test_short_mirrors_long():
    from src.risk.conviction_bar import seat_objections

    verdicts = [
        _v("technical", "TSLA", direction="bearish"),
        _v("smart_money", "TSLA", direction="bearish"),
        _v("news", "TSLA", direction="bullish"),
    ]
    assert own_bar_block_reason(verdicts, direction="bearish") is None
    assert [(o["seat"], o["direction"]) for o in seat_objections(verdicts, direction="bearish")] == [
        ("news", "bullish")
    ]
    only_macro = [_v("technical", "TSLA", direction="bearish"), _macro("TSLA", sector_specific=True)]
    assert "no name-level seat" in own_bar_block_reason(only_macro, direction="bearish")


class _pm_with:
    """Calls the manager's objection gate with this session's seat verdicts."""

    def __init__(self, verdicts):
        self.verdicts = verdicts

    def _drop_unanswered_objections(self, decision, **kw):
        return PortfolioManagerAgent._drop_unanswered_objections(decision, seat_verdicts=self.verdicts, **kw)


def test_buy_over_an_unanswered_objection_is_refused_objection_unanswered():
    pm = _pm_with(_NAME_OPPOSED)
    dropped: list[dict] = []
    decision = _decision([_buy_target("NVDA", conflict_source=None)], conflicts="none")
    result = pm._drop_unanswered_objections(decision, positions=[], total_value=100_000, dropped=dropped)
    assert result.targets == []
    assert dropped[0]["symbol"] == "NVDA" and dropped[0]["gate"] == "objection_unanswered"
    assert dropped[0]["objecting_seats"] == ["earnings"]


def test_an_accept_answer_still_refuses_and_an_overrule_admits():
    def target(decision_word):
        t = _buy_target("NVDA", conflict_source=None)
        t["objection_answers"] = [
            {"seat": "earnings", "objection_direction": "bearish", "answer_text": "why", "decision": decision_word}
        ]
        return t

    pm = _pm_with(_NAME_OPPOSED)
    accepted = pm._drop_unanswered_objections(
        _decision([target("accept")], conflicts="none"), positions=[], total_value=100_000, dropped=[]
    )
    assert accepted.targets == []
    overruled = pm._drop_unanswered_objections(
        _decision([target("overrule")], conflicts="none"), positions=[], total_value=100_000, dropped=[]
    )
    assert [t.symbol for t in overruled.targets] == ["NVDA"]


def test_entry_floor_no_longer_subtracts_name_level_seats_or_macro():
    from src.risk.rules import entry_floor_score, signed_source_score

    sources = {"technical": "bullish", "news": "bullish", "earnings": "bearish", "macro": "bearish"}
    assert signed_source_score("NVDA", sources, "long") == 0  # the retired sum refused this
    assert entry_floor_score("NVDA", sources, "long") == 2  # chart for + news for
    assert entry_floor_score("NVDA", {"technical": "bearish", "news": "bullish"}, "long") == 0
