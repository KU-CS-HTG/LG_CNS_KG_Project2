"""C: src/generate/*. B와 LLM 없이 시험할 수 있는 부분은 fake_search·fake_conditions로 시험한다.

OpenAI 키가 필요한 테스트(실제 답변 생성)는 OPENAI_API_KEY가 없으면 skip된다.
"""
import pytest

from src.common import config
from src.common.schema import Answer
from src.generate import condition_extractor, evidence, generator
from src.generate.citation import DISCLAIMER, NO_EVIDENCE_MESSAGE, build_citations

from tests.fixtures import load_search_cases

CASES = {c["id"]: c for c in load_search_cases()}
QUESTION_WITH_RESULTS = CASES["sr1"]["question"]
QUESTION_NO_EVIDENCE = CASES["sr2"]["question"]


# --- 조건 -> 필터 (C -> B 인계) ------------------------------------------------
def test_to_filters_keeps_given_and_omits_missing():
    f = condition_extractor.to_filters(CASES["sr1"]["conditions"])
    assert f["age"] == 27 and f["region"] == "서울" and f["field"] == "주거"
    empty = condition_extractor.to_filters({})
    assert "age" not in empty and "region" not in empty        # 못 찾은 항목은 키 생략


# --- 근거 선별 (B -> C 인계를 받는 곳) ----------------------------------------
def test_select_evidence_groups_by_service():
    grouped = evidence.select_evidence(CASES["sr1"]["results"])
    assert 1 <= len(grouped) <= config.MAX_RECOMMENDATIONS
    for service_id, chunks in grouped.items():
        assert chunks and all(c.service_id == service_id for c in chunks)


def test_select_evidence_limits_services(sample_chunks):
    grouped = evidence.select_evidence(sample_chunks)           # 12개 제도를 한꺼번에 넣어도
    assert len(grouped) <= config.MAX_RECOMMENDATIONS            # 3~5개 제도로 제한


# --- 인용 -----------------------------------------------------------------------
def test_build_citations_fields():
    chunks = CASES["sr1"]["results"]
    citations = build_citations(chunks)
    assert citations
    for cit in citations:
        assert cit.service_name and cit.agency and cit.detail_url
        assert cit.quote and any(cit.quote in c.text for c in chunks)   # 근거 문구는 실제 조각에서 온다


# --- 근거 없음: LLM 없이도 시험 가능 -----------------------------------------
def test_generate_answer_without_evidence():
    answer = generator.generate_answer(QUESTION_NO_EVIDENCE, {})
    assert isinstance(answer, Answer)
    assert answer.no_evidence is True and answer.recommendations == []
    assert NO_EVIDENCE_MESSAGE in answer.text                    # 모른다고 답하고 담당 기관 안내


def test_answer_question_no_evidence_end_to_end(fake_search, fake_conditions):
    case = fake_search("sr2")
    fake_conditions(case)
    answer = generator.answer_question(case["question"])
    assert answer.no_evidence is True
    assert case["calls"], "search가 호출되지 않음"                  # 결과가 없으면 필터를 풀어 재시도할 수 있다
    assert all(isinstance(c["filters"], dict) for c in case["calls"])


# --- 실제 생성(LLM 필요) -------------------------------------------------------
@pytest.mark.skipif(not config.OPENAI_API_KEY, reason="OPENAI_API_KEY 없음")
def test_answer_question_with_results(fake_search, fake_conditions):
    case = fake_search("sr1")
    fake_conditions(case)
    answer = generator.answer_question(case["question"])
    assert answer.no_evidence is False
    assert 1 <= len(answer.recommendations) <= config.MAX_RECOMMENDATIONS
    assert DISCLAIMER in answer.text                             # 담당 기관 확인 안내
    assert case["calls"][0]["filters"].get("region") == "서울"    # C가 조건을 B에게 제대로 넘겼는가
    for rec in answer.recommendations:
        assert rec.citations and all(c.detail_url for c in rec.citations)
    unclear = [r for r in answer.recommendations if "생활안정" in r.service_name]   # SAMPLE011: 값이 빈 제도
    assert all(r.needs_check for r in unclear)                   # 단정하지 않고 '확인 필요'

