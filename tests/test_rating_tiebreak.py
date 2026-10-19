"""n06: the Technical rating rung breaks ties; it is never added to the score.

Before n06 a Technical buy+medium scored 1.0 while a News medium scored 0.5,
double weight for one seat against the owner's 25 Sep "every seat counts the
same" ruling.
"""

from src.models import AnalystVerdict, VerdictEvidence, rating_rung_strength
from src.verdicts import rank_verdicts, score_verdict


def _verdict(seat, symbol, rating=None, conviction="medium", direction="bullish"):
    magnitude = rating_rung_strength(rating) if rating else None
    return AnalystVerdict(
        seat=seat,
        symbol=symbol,
        direction=direction,
        magnitude=magnitude,
        conviction=conviction,
        evidence=[VerdictEvidence(label="ev", text="a checkable observed fact")],
        invalidation="closes below 95",
    )


def test_technical_buy_scores_equal_to_news_of_same_conviction():
    tech = _verdict("technical", "ZZZ", rating="buy")
    news = _verdict("news", "ZZZ")
    assert score_verdict(tech) == score_verdict(news)


def test_strong_buy_ranks_above_buy_at_equal_conviction():
    ranked = rank_verdicts(
        [
            _verdict("technical", "AAA", rating="buy"),
            _verdict("technical", "ZZZ", rating="strong_buy"),
        ]
    )
    assert [c.symbol for c in ranked] == ["ZZZ", "AAA"]
    assert ranked[0].score == ranked[1].score


def test_strong_sell_ranks_above_sell_at_equal_conviction():
    ranked = rank_verdicts(
        [
            _verdict("technical", "AAA", rating="sell", direction="bearish"),
            _verdict("technical", "ZZZ", rating="strong_sell", direction="bearish"),
        ]
    )
    assert [c.symbol for c in ranked] == ["ZZZ", "AAA"]


def test_stated_conviction_no_longer_ranks_so_strong_buy_wins_the_tie():
    """2026-10-19: conviction is not scored, so one buy and one strong_buy
    from the same seat tie on count and the strong rung breaks the tie."""
    ranked = rank_verdicts(
        [
            _verdict("technical", "AAA", rating="strong_buy", conviction="medium"),
            _verdict("technical", "ZZZ", rating="buy", conviction="high"),
        ]
    )
    assert [c.symbol for c in ranked] == ["AAA", "ZZZ"]
