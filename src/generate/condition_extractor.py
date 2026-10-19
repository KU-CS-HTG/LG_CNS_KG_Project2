"""질문 -> 정형 조건 추출(LLM). 나이·거주지·상황·관심 분야. 못 찾은 항목은 키를 생략한다."""
from __future__ import annotations


def extract_conditions(question: str) -> dict:
    """예: {"age": 27, "region": "서울", "situation": ["구직"], "field": "주거"}"""
    raise NotImplementedError
