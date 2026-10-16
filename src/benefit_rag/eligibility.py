"""규칙 추출 없이 검증된 조건만 평가하는 최소 순수 함수. 공식 자격 결정이 아님."""

from enum import StrEnum


class ConditionResult(StrEnum):
    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    NEEDS_CONFIRMATION = "needs_confirmation"


def evaluate_age(
    age: int | None,
    minimum: int | None,
    maximum: int | None,
    *,
    user_confirmed: bool,
    rule_validated: bool,
) -> ConditionResult:
    values = [v for v in (age, minimum, maximum) if v is not None]
    if (
        not user_confirmed
        or not rule_validated
        or age is None
        or (minimum is None and maximum is None)
        or any(type(v) is not int or v < 0 for v in values)
        or (minimum is not None and maximum is not None and minimum > maximum)
    ):
        return ConditionResult.NEEDS_CONFIRMATION
    if (minimum is not None and age < minimum) or (maximum is not None and age > maximum):
        return ConditionResult.NOT_SATISFIED
    return ConditionResult.SATISFIED


def aggregate(results: list[ConditionResult], *, operator: str) -> ConditionResult:
    if operator not in {"AND", "OR"}:
        raise ValueError("operator must be AND or OR")
    if not results:
        return ConditionResult.NEEDS_CONFIRMATION
    if operator == "AND":
        if ConditionResult.NOT_SATISFIED in results:
            return ConditionResult.NOT_SATISFIED
        if all(r == ConditionResult.SATISFIED for r in results):
            return ConditionResult.SATISFIED
    else:
        if ConditionResult.SATISFIED in results:
            return ConditionResult.SATISFIED
        if all(r == ConditionResult.NOT_SATISFIED for r in results):
            return ConditionResult.NOT_SATISFIED
    return ConditionResult.NEEDS_CONFIRMATION
