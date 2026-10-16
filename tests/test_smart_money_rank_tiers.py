"""The tier tuples must order exactly as the old made-up rank numbers did."""

import itertools

import pytest

from src.agents import smart_money_analyst as sma
from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
from tests.test_smart_money import _insider

# Reference: the old dict values, hard-coded. Unknown keys scored 0.
OLD_ROLE = {"actionable": 3, "confirmatory": 2, "contradictory": 1, "historical": 0}
OLD_FRESH = {"fresh": 2, "delayed": 1, "stale": 0}
OLD_CLASS = {"opportunistic": 2, "": 1, "indeterminate": 1, "routine": 0}

CASES = [
    (sma._ROLE_TIERS, OLD_ROLE),
    (sma._FRESHNESS_TIERS, OLD_FRESH),
    (sma._SIGNAL_CLASS_TIERS, OLD_CLASS),
]


def _cmp(a, b):
    return (a > b) - (a < b)


def assert_same_order(tiers, old):
    keys = [*old, "zz_unknown"]
    for x, y in itertools.product(keys, keys):
        new = _cmp(-sma._tier_position(tiers, x), -sma._tier_position(tiers, y))
        assert new == _cmp(old.get(x, 0), old.get(y, 0)), (x, y)


@pytest.mark.parametrize(("tiers", "old"), CASES)
def test_tier_order_matches_old_numbers(tiers, old):
    assert_same_order(tiers, old)


def test_swapping_two_tiers_breaks_the_order_check():
    tiers = sma._ROLE_TIERS
    swapped = (tiers[1], tiers[0], tiers[2])
    with pytest.raises(AssertionError):
        assert_same_order(swapped, OLD_ROLE)


def _fixtures():
    base = _insider(symbol="ZZZ")
    roles = [*OLD_ROLE, "zz_unknown"]
    classes = [*OLD_CLASS, "zz_unknown"]
    fresh = [*OLD_FRESH, "zz_unknown"]
    return [
        base.model_copy(
            update={
                "economic_role": r,
                "signal_class": c,
                "freshness": f,
                "accession_number": f"acc-{index:04d}",
            }
        )
        for index, (r, c, f) in enumerate(itertools.product(roles, classes, fresh))
    ]


def test_transaction_rank_orders_zzz_fixture_like_the_old_numbers():
    rows = _fixtures()

    def old_key(row):
        new = SmartMoneyAnalystAgent._transaction_rank(row)
        old = (
            -OLD_ROLE.get(row.economic_role, 0),
            -OLD_CLASS.get(row.signal_class, 0),
            -OLD_FRESH.get(row.freshness, 0),
        )
        return (*new[:2], *old, *new[5:])

    old_order = [r.accession_number for r in sorted(rows, key=old_key)]
    new_order = [r.accession_number for r in sorted(rows, key=SmartMoneyAnalystAgent._transaction_rank)]
    assert new_order == old_order


def test_symbol_rank_orders_zzz_fixture_like_the_old_numbers():
    groups = [list(pair) for pair in itertools.combinations(_fixtures()[::7], 2)]

    def old_key(i):
        rows = groups[i]
        new = SmartMoneyAnalystAgent._symbol_rank("ZZZ", rows)
        return (
            *new[:3],
            -max(OLD_ROLE.get(r.economic_role, 0) for r in rows),
            -max(OLD_CLASS.get(r.signal_class, 0) for r in rows),
            -max(OLD_FRESH.get(r.freshness, 0) for r in rows),
            *new[6:],
        )

    indexes = range(len(groups))
    old_order = sorted(indexes, key=old_key)
    new_order = sorted(indexes, key=lambda i: SmartMoneyAnalystAgent._symbol_rank("ZZZ", groups[i]))
    assert new_order == old_order
