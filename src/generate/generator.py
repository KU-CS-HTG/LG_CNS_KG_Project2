"""⑥ 생성 - 검색된 근거 안에서만 답변. 근거 없으면 모른다고 답하고 기관 안내.

규칙(CLAUDE.md '답변 생성 규칙'): 자격 불명확 시 '확인 필요', 한국어, 출처 필수.
"""
from __future__ import annotations

from src.common.schema import Answer, Chunk


def generate_answer(question: str, chunks: list[Chunk]) -> Answer:
    raise NotImplementedError


def answer_question(question: str) -> Answer:
    """전체 온라인 흐름: 조건 추출 -> 검색(필터 완화 재시도 포함) -> 생성."""
    raise NotImplementedError
