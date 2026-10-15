"""Board item 225: citations are resolved, not just counted."""

from pathlib import Path

import pytest

from src.ledger_citations import broken_citations


def test_a_citation_whose_text_moved_or_changed_is_rejected(tmp_path: Path) -> None:
    """Item 225. The old guard passed any line that merely existed."""
    (tmp_path / "src").mkdir()
    module = tmp_path / "src" / "m.py"
    module.write_text("X = 1\n\n\nclass A:\n    def f(self):\n        return 2\n")

    def reasons(source: str) -> list[str]:
        ledger = {"s": {"status": "sourced", "source": source}}
        return [why for _, why, _ in broken_citations(ledger, tmp_path)]

    assert reasons("src/m.py::A.f and src/m.py::X") == []
    assert reasons("src/m.py@`return   2`") == []
    assert reasons("src/m.py:6")  # a bare line is unverifiable by construction
    assert reasons("src/m.py::A.g")  # symbol gone
    assert reasons("src/m.py@`return 3`")  # text changed
    # the file is reshuffled: lines move, the symbol citation still holds,
    # and a snippet citation that no longer exists is caught
    module.write_text("\n" * 40 + "class A:\n    def f(self):\n        return 9\n")
    assert reasons("src/m.py::A.f") == []
    assert reasons("src/m.py@`return 2`")


def test_a_citation_pointing_at_nothing_is_reported() -> None:
    """Rule 7, firing. `broken_citations` is checked directly rather than
    through a fixture tree, because a fixture has no `docs/` and every real
    citation would read as missing there — a test that passes for the wrong
    reason is worse than no test.
    """
    root = Path(__file__).resolve().parent.parent
    invented = {
        "src.risk.rules.MAX_HEAT_PCT": {
            "status": "sourced",
            "source": "the measured matrix in docs/DOES_NOT_EXIST.md:12",
        },
        "src.risk.rules.PAST_EOF": {
            "status": "sourced",
            "source": "see src/number_sources.py:999999",
        },
        "src.risk.rules.FINE": {
            "status": "sourced",
            "source": "see src/number_sources.py::audit",
        },
    }
    reported = {site_id for site_id, _, _ in broken_citations(invented, root)}
    assert reported == {"src.risk.rules.MAX_HEAT_PCT", "src.risk.rules.PAST_EOF"}


@pytest.mark.parametrize(
    "cite",
    [
        "src/verdicts.py::__all__",
        "src/risk/trailing.py::__all__",
        "src/risk/constants.py@`import math`",
        "src/data/technical.py@`from src.models import OHLCV, TechnicalIndicator`",
        "src/verdicts.py@`(five parallel literature reviews, 2026-09-03, W`",
        "src/verdicts.py@`analyst prompts are) — and let it be overridden`",
        "src/verdicts.py@`weight applies there).** The original rule was`",
    ],
)
def test_a_citation_that_cannot_substantiate_anything_is_refused(cite: str) -> None:
    """Item 232: resolving is not substantiating. These are the measured
    bystander pins; each must stay refused if anybody writes it back."""
    root = Path(__file__).resolve().parent.parent
    row = {"x.y": {"status": "sourced", "source": f"see {cite} for it"}}
    assert [s for s, _, _ in broken_citations(row, root)] == ["x.y"]


def test_a_citation_that_substantiates_is_accepted() -> None:
    root = Path(__file__).resolve().parent.parent
    row = {"x.y": {"status": "sourced", "source": "see src/verdicts.py::CONVICTION_SCORE"}}
    assert broken_citations(row, root) == []
