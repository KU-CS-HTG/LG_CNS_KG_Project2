"""⑥ 생성 - 검색된 근거 안에서만 답변 (gpt-4o-mini + 근거 한정 프롬프트 + 인용).

규칙(CLAUDE.md '답변 생성 규칙'): 근거 없으면 모른다고 답하고 기관 안내, 자격 불명확하면 '확인 필요',
모든 답변에 서비스명·소관기관·근거 문구·상세 링크와 DISCLAIMER, 사용자 문구는 한국어.
"""
from __future__ import annotations

from src.common.schema import Answer, Chunk


def generate_answer(question: str, evidence: dict[str, list[Chunk]]) -> Answer:
    raise NotImplementedError


def answer_question(question: str) -> Answer:
    """온라인 전체 흐름: 조건 추출 -> 검색(필터 완화 재시도) -> 근거 선별 -> 생성. app은 이 함수만 호출한다."""
    raise NotImplementedError
