"""⑥ 생성 - 검색된 근거 안에서만 답변 (gpt-4o-mini + 근거 한정 프롬프트 + 인용).

규칙(CLAUDE.md '답변 생성 규칙'): 근거 없으면 모른다고 답하고 기관 안내, 자격 불명확하면 '확인 필요',
모든 답변에 서비스명·소관기관·근거 문구·상세 링크와 DISCLAIMER, 사용자 문구는 한국어.
"""
from __future__ import annotations

from src.common import config
from src.common.schema import Answer, Chunk
from src.generate import condition_extractor
from src.generate import evidence as evidence_mod
from src.generate.citation import NO_EVIDENCE_MESSAGE
from src.retrieve import hybrid

# 테스트 규칙: B가 끝나기 전에도 시험할 수 있도록(tests/conftest.py의 fake_search, fake_conditions)
# 다른 모듈의 함수는 `from src.retrieve import hybrid; hybrid.search(...)`,
# `condition_extractor.extract_conditions(...)`처럼 모듈 속성으로 호출한다. (`from ... import search` 금지)


def generate_answer(question: str, evidence: dict[str, list[Chunk]]) -> Answer:
    if not evidence:
        return Answer(text=NO_EVIDENCE_MESSAGE, no_evidence=True)
    raise NotImplementedError   # 7단계: 근거 있음 경로 (LLM)


def answer_question(question: str) -> Answer:
    """온라인 전체 흐름: 조건 추출 -> 검색(필터 완화 재시도) -> 근거 선별 -> 생성. app은 이 함수만 호출한다.

    필터 완화(filters.relax) 재검색은 B의 hybrid.search 안에서 한다 (중복 구현하지 않음).
    """
    conditions = condition_extractor.extract_conditions(question)
    filters = condition_extractor.to_filters(conditions)
    chunks = hybrid.search(question, filters, k=config.DEFAULT_TOP_K)
    selected = evidence_mod.select_evidence(chunks)
    return generate_answer(question, selected)
