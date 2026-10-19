"""질문 -> 정형 조건 추출(LLM). 나이·거주지·상황·관심 분야 (기획서 8.1 질문 개선).

못 찾은 항목은 키를 생략한다 (추측 금지). 표현 차이로 성능이 흔들리면 Query Rewrite / Multi Query를 추가한다.
"""
from __future__ import annotations

from src.common.schema import Filters


def extract_conditions(question: str) -> dict:
    """예: {"age": 27, "region": "서울", "situation": ["구직"], "field": "주거"}"""
    raise NotImplementedError


def to_filters(conditions: dict) -> Filters:
    """추출된 조건을 B의 검색 필터로 변환."""
    raise NotImplementedError
