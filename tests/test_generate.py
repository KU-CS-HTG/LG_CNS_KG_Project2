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


# --- 조건 추출: LLM 출력 정리 (LLM 없이) -----------------------------------------
def test_normalize_conditions_drops_invalid_values():
    raw = {"age": "27", "region": "서울특별시", "field": "주거", "situation": ["구직", "우주여행", "구직"],
           "hobby": "등산"}
    assert condition_extractor._normalize_conditions(raw) == {
        "age": 27, "region": "서울", "field": "주거", "situation": ["구직"]}   # 목록 밖 값·모르는 키는 버림
    assert condition_extractor._normalize_conditions({"region": "화성", "field": "여행", "age": True}) == {}


def test_normalize_conditions_birth_year_to_age():
    from datetime import date
    got = condition_extractor._normalize_conditions({"birth_year": 1999}, today=date(2026, 10, 19))
    assert got == {"age": 27}


def test_normalize_conditions_grounds_age_in_question():
    from datetime import date
    norm = condition_extractor._normalize_conditions
    assert norm({"age": 28}, question="20대 후반인데 창업 지원금 있나요?") == {}           # 추측한 나이는 버림
    assert norm({"age": 26}, today=date(2026, 10, 19), question="97년생이에요") == {"age": 29}  # 출생연도는 코드가 계산
    assert norm({"age": 27}, question="서울에 사는 27살이에요") == {"age": 27}


def test_extract_conditions_returns_empty_on_llm_error(monkeypatch):
    def _fail(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", _fail)
    assert condition_extractor.extract_conditions("서울 사는 27살이에요") == {}   # 서비스가 멈추지 않는다


@pytest.mark.skipif(not config.OPENAI_API_KEY, reason="OPENAI_API_KEY 없음")
def test_extract_conditions_with_llm():
    got = condition_extractor.extract_conditions(QUESTION_WITH_RESULTS)
    assert got.get("age") == 27 and got.get("region") == "서울"
    assert got.get("field") == "주거" and "구직" in got.get("situation", [])
    assert condition_extractor.extract_conditions(QUESTION_NO_EVIDENCE).get("age") is None   # 추측 금지


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



# --- 생성: LLM 출력을 가짜로 고정 (LLM 없이) -------------------------------------
def _fake_llm(monkeypatch, out):
    def _chat(system, user):
        if isinstance(out, Exception):
            raise out
        return out
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", _chat)


def test_generate_answer_excludes_not_eligible_and_fills_from_chunks(monkeypatch):
    ev = evidence.select_evidence(CASES["sr1"]["results"])
    first, second = list(ev)[:2]
    _fake_llm(monkeypatch, {"situation": "서울 거주, 27세", "follow_up": "가구 소득은 어느 정도인가요?", "services": [
        {"service_id": first, "applicable": "가능", "reason": "이유", "benefit": "혜택", "how_to_apply": "온라인",
         "needs_check": []},
        {"service_id": second, "applicable": "해당 없음"},          # 명백히 맞지 않으면 후보에서 뺀다
    ]})
    answer = generator.generate_answer(QUESTION_WITH_RESULTS, ev)
    names = [r.service_name for r in answer.recommendations]
    assert ev[second][0].service_name not in names and len(names) == len(ev) - 1
    rec = answer.recommendations[0]
    assert rec.service_name == ev[first][0].service_name and rec.agency == ev[first][0].metadata["agency"]  # 코드가 채움
    assert rec.citations and all(any(c.quote in ch.text for ch in ev[first]) for c in rec.citations)
    assert "신청기한" in rec.how_to_apply
    assert "가구 소득" in answer.text and DISCLAIMER in answer.text


def test_generate_answer_falls_back_when_llm_fails(monkeypatch):
    ev = evidence.select_evidence(CASES["sr1"]["results"])
    _fake_llm(monkeypatch, RuntimeError("network down"))
    answer = generator.generate_answer(QUESTION_WITH_RESULTS, ev)
    assert answer.no_evidence is False and len(answer.recommendations) == len(ev)   # 원문만으로 안내
    assert all(r.citations and r.needs_check for r in answer.recommendations)        # 판단 못 했으니 확인 필요
    assert DISCLAIMER in answer.text


def test_generate_answer_all_excluded_is_no_evidence(monkeypatch):
    ev = evidence.select_evidence(CASES["sr1"]["results"])
    _fake_llm(monkeypatch, {"services": [{"service_id": s, "applicable": "해당 없음"} for s in ev]})
    answer = generator.generate_answer(QUESTION_WITH_RESULTS, ev)
    assert answer.no_evidence is True and answer.recommendations == [] and NO_EVIDENCE_MESSAGE in answer.text


def test_reason_uses_only_phrases_from_question(monkeypatch):
    ev = evidence.select_evidence(CASES["sr1"]["results"])
    sid = list(ev)[0]
    _fake_llm(monkeypatch, {"services": [{"service_id": sid, "applicable": "가능", "matches": [
        {"said": "서울에 사는", "condition": "서울에 거주하며"},
        {"said": "저소득 구직자", "condition": "저소득 구직자"},     # 질문에 없는 구절 -> 버림
    ]}]})
    rec = generator.generate_answer(QUESTION_WITH_RESULTS, {sid: ev[sid]}).recommendations[0]
    assert "서울에 사는" in rec.reason and "저소득" not in rec.reason


def test_no_grounded_match_downgrades_to_needs_check(monkeypatch):
    ev = evidence.select_evidence(CASES["sr1"]["results"])
    sid = list(ev)[0]
    _fake_llm(monkeypatch, {"services": [{"service_id": sid, "applicable": "가능", "needs_check": [],
                                          "matches": [{"said": "무주택 청년", "condition": "무주택 청년"}]}]})
    rec = generator.generate_answer(QUESTION_WITH_RESULTS, {sid: ev[sid]}).recommendations[0]
    assert rec.needs_check                                            # '가능'이라 하지 않고 확인 필요
