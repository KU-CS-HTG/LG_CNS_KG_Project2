from itertools import product

import pytest

from benefit_rag.eligibility import ConditionResult as R
from benefit_rag.eligibility import aggregate, evaluate_age


@pytest.mark.parametrize(
    "age,expected",
    [
        (18, R.NOT_SATISFIED),
        (19, R.SATISFIED),
        (34, R.SATISFIED),
        (35, R.NOT_SATISFIED),
        (None, R.NEEDS_CONFIRMATION),
    ],
)
def test_age_boundaries(age, expected):
    assert evaluate_age(age, 19, 34, user_confirmed=True, rule_validated=True) == expected


@pytest.mark.parametrize("confirmed,validated", [(False, True), (True, False), (False, False)])
def test_unverified_information_stays_unknown(confirmed, validated):
    assert evaluate_age(25, 19, 34, user_confirmed=confirmed, rule_validated=validated) == (
        R.NEEDS_CONFIRMATION
    )


@pytest.mark.parametrize(
    "age,minimum,maximum", [(True, 19, 34), (-1, 19, 34), (25, 34, 19), (25, None, None)]
)
def test_invalid_age_or_rule_is_unknown(age, minimum, maximum):
    assert evaluate_age(age, minimum, maximum, user_confirmed=True, rule_validated=True) == (
        R.NEEDS_CONFIRMATION
    )


@pytest.mark.parametrize("a,b", list(product(list(R), repeat=2)))
def test_and_or_truth_tables(a, b):
    order = {R.NOT_SATISFIED: 0, R.NEEDS_CONFIRMATION: 1, R.SATISFIED: 2}
    assert aggregate([a, b], operator="AND") == min([a, b], key=order.get)
    assert aggregate([a, b], operator="OR") == max([a, b], key=order.get)


def test_nested_branches_empty_and_invalid_operator():
    branch = aggregate([R.NOT_SATISFIED, R.SATISFIED], operator="OR")
    assert aggregate([branch, R.NEEDS_CONFIRMATION], operator="AND") == R.NEEDS_CONFIRMATION
    assert aggregate([], operator="AND") == R.NEEDS_CONFIRMATION
    with pytest.raises(ValueError):
        aggregate([], operator="XOR")
