"""질문 -> 정형 조건 추출(LLM). 나이·거주지·상황·관심 분야 (기획서 8.1 질문 개선).

못 찾은 항목은 키를 생략한다 (추측 금지). 표현 차이로 성능이 흔들리면 Query Rewrite / Multi Query를 추가한다.
"""
from __future__ import annotations

from datetime import date

from src.common.schema import Filters


def extract_conditions(question: str) -> dict:
    """예: {"age": 27, "region": "서울", "situation": ["구직"], "field": "주거"}"""
    raise NotImplementedError


def to_filters(conditions: dict, today: str | None = None) -> Filters:
    """추출된 조건을 B의 검색 필터로 변환. 값이 비었거나 형식이 맞지 않는 항목은 키를 생략한다."""
    filters: Filters = {}

    age = conditions.get("age")
    if isinstance(age, str) and age.strip().isdigit():
        age = int(age)
    if isinstance(age, int) and not isinstance(age, bool) and 0 <= age <= 120:
        filters["age"] = age

    for key in ("region", "field"):
        value = conditions.get(key)
        if isinstance(value, str) and value.strip():
            filters[key] = value.strip()

    situation = conditions.get("situation")
    if isinstance(situation, str):
        situation = [situation]
    if isinstance(situation, list):
        situation = [s.strip() for s in situation if isinstance(s, str) and s.strip()]
        if situation:
            filters["situation"] = situation

    filters["today"] = today or conditions.get("today") or date.today().isoformat()
    return filters
