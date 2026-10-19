"""C: src/generate/*. B와 LLM 없이 시험할 수 있는 부분은 fake_search·fake_conditions로 시험한다.

OpenAI 키가 필요한 테스트(실제 답변 생성)는 OPENAI_API_KEY가 없으면 skip된다.
"""
import pytest

from src.common import config
from src.common.schema import Answer
from src.generate import condition_extractor, evidence, generator
from src.generate.citation import DISCLAIMER, NO_EVIDENCE_MESSAGE, SCOPE_NOTICE, build_citations

from tests.fixtures import load_search_cases

CASES = {c["id"]: c for c in load_search_cases()}
QUESTION_WITH_RESULTS = CASES["sr1"]["question"]
QUESTION_NO_EVIDENCE = CASES["sr2"]["question"]


@pytest.fixture(autouse=True)
def _no_real_store(request, monkeypatch):
    """C 테스트가 실제 저장소(data/vectorstore)를 열지 않게, 서비스 전체 항목 조회를 기본으로 '없음'으로 둔다.
    B의 실제 색인(indexed_sample)을 쓰는 통합 테스트는 그대로 둔다."""
    if "indexed_sample" not in request.fixturenames:
        monkeypatch.setattr("src.index.vector_store.get_service_chunks", lambda service_id: [])
        # C는 완화 정보 때문에 search_with_info를 부른다. 공용 fake_search는 search만 바꾸므로,
        # search_with_info가 (가짜일 수 있는) search를 그대로 쓰게 이어 준다 (완화 없음으로).
        from src.retrieve import hybrid
        monkeypatch.setattr(hybrid, "search_with_info", lambda q, f, k=5, mode="hybrid_filter":
                            hybrid.SearchResult(hybrid.search(q, f, k, mode), [], dict(f), False))


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
    assert SCOPE_NOTICE in answer.text                           # 청년 대상 범위 안내 (README 1.1)


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
        {"service_id": second, "applicable": "해당 없음",              # 질문에 근거가 있는 불일치면 후보에서 뺀다
         "mismatch": {"said": "서울에 사는", "condition": "부산에 거주"}},
    ] + [{"service_id": s, "applicable": "확인 필요"} for s in list(ev)[2:]]})
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
    _fake_llm(monkeypatch, {"services": [{"service_id": s, "applicable": "관련 없음"} for s in ev]})
    answer = generator.generate_answer(QUESTION_WITH_RESULTS, ev)
    assert answer.no_evidence is True and answer.recommendations == [] and NO_EVIDENCE_MESSAGE in answer.text
    assert SCOPE_NOTICE in answer.text


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


# --- 답변 평가 (eval/answer_eval.py, C 담당) ---------------------------------------
def _sample_answer(monkeypatch):
    """sr1 근거로 LLM 없이 만든 답변 (LLM 실패 경로 = 조각 원문만 사용) + 근거 문자열."""
    ev = evidence.select_evidence(CASES["sr1"]["results"])
    _fake_llm(monkeypatch, RuntimeError("no llm"))
    answer = generator.generate_answer(QUESTION_WITH_RESULTS, ev)
    return answer, "\n".join(c.text for cs in ev.values() for c in cs)


def test_citation_accuracy_checks_quotes_against_evidence(monkeypatch):
    from dataclasses import replace
    from eval import answer_eval
    answer, ev_text = _sample_answer(monkeypatch)
    assert answer_eval.citation_accuracy(answer, ev_text) == 1.0            # 인용은 조각에서 만들므로 모두 정확
    rec = answer.recommendations[0]
    fake = replace(rec, citations=[replace(rec.citations[0], quote="월 100만 원을 평생 지급")])
    broken = Answer(text=answer.text, recommendations=[fake] + answer.recommendations[1:])
    n = sum(len(r.citations) for r in broken.recommendations)
    assert answer_eval.citation_accuracy(broken, ev_text) == (n - 1) / n     # 근거에 없는 인용 1개는 틀림
    assert answer_eval.citation_accuracy(Answer(text="", no_evidence=True), "") == 1.0


def test_correctness_for_no_answer_questions():
    from eval import answer_eval
    q = {"question": QUESTION_NO_EVIDENCE, "expect_no_answer": True}
    assert answer_eval.answer_correctness(Answer(text=NO_EVIDENCE_MESSAGE, no_evidence=True), q) == 1.0
    q2 = {"question": QUESTION_WITH_RESULTS, "expect_no_answer": False, "expected_answer": "주거 지원 안내"}
    assert answer_eval.answer_correctness(Answer(text=NO_EVIDENCE_MESSAGE, no_evidence=True), q2) == 0.0


def test_faithfulness_counts_supported_claims(monkeypatch):
    from eval import answer_eval
    answer, ev_text = _sample_answer(monkeypatch)
    n = len(answer_eval._claims(answer))
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", lambda s, u: {
        "verdicts": [{"id": i, "supported": i != 1} for i in range(1, n + 1)]})
    assert answer_eval.faithfulness(answer, ev_text) == (n - 1) / n
    assert answer_eval.faithfulness(Answer(text=NO_EVIDENCE_MESSAGE, no_evidence=True), "") == 1.0


@pytest.mark.skipif(not config.OPENAI_API_KEY, reason="OPENAI_API_KEY 없음")
def test_faithfulness_with_llm_catches_made_up_claim():
    from eval import answer_eval
    from src.common.schema import Recommendation
    ev_text = "청년월세 지원(서울)\n서울에 거주하며 월세로 사는 만 19~34세 무주택 청년\n청년월세 지원(서울)\n월 월세의 일부를 일정 기간 지원"
    def ans(benefit):
        return Answer(text="", recommendations=[Recommendation(
            service_name="청년월세 지원(서울)", agency="서울특별시", reason="", benefit=benefit, how_to_apply="")])
    assert answer_eval.faithfulness(ans("월세의 일부를 일정 기간 지원합니다."), ev_text) == 1.0
    assert answer_eval.faithfulness(ans("매달 50만 원을 2년 동안 현금으로 지급합니다."), ev_text) == 0.0


def test_not_eligible_without_question_evidence_is_kept_as_needs_check(monkeypatch):
    ev = evidence.select_evidence(CASES["sr1"]["results"])
    sid = list(ev)[0]
    _fake_llm(monkeypatch, {"services": [{"service_id": sid, "applicable": "해당 없음",
                                          "mismatch": {"said": "부산 거주", "condition": "서울에 거주"}}]})  # 질문에 없는 구절
    answer = generator.generate_answer(QUESTION_WITH_RESULTS, {sid: ev[sid]})
    assert len(answer.recommendations) == 1 and answer.recommendations[0].needs_check   # 빼지 않고 확인 필요


def test_services_missing_from_llm_verdict_are_dropped(monkeypatch):
    ev = evidence.select_evidence(CASES["sr1"]["results"])
    _fake_llm(monkeypatch, {"services": []})                      # 판정은 왔지만 관련 있는 제도가 없음
    assert generator.generate_answer(QUESTION_WITH_RESULTS, ev).no_evidence is True


def test_vague_income_criteria_stays_needs_check(monkeypatch, sample_chunks):
    chunks = [c for c in sample_chunks if c.service_id == "SAMPLE001"]   # 선정기준: '소득이 기준 이하' (금액 없음)
    _fake_llm(monkeypatch, {"services": [{"service_id": "SAMPLE001", "applicable": "가능", "needs_check": [],
                                          "matches": [{"said": "서울에 사는", "condition": "서울에 거주하며"}]}]})
    q = QUESTION_WITH_RESULTS + " 소득은 월 150만 원이에요"
    rec = generator.generate_answer(q, {"SAMPLE001": chunks}).recommendations[0]
    assert any("소득" in n for n in rec.needs_check)              # 사용자가 소득을 밝혀도 기준 금액이 없으면 단정 안 함



# --- 서비스 전체 항목 (B의 vector_store.get_service_chunks) ------------------------
def test_select_evidence_expands_to_all_sections(monkeypatch, sample_chunks):
    by_service = {}
    for c in sample_chunks:
        by_service.setdefault(c.service_id, []).append(c)
    monkeypatch.setattr("src.index.vector_store.get_service_chunks", lambda sid: list(by_service.get(sid, [])))
    grouped = evidence.select_evidence(CASES["sr1"]["results"])          # 검색 결과는 서비스당 조각 1개
    for sid, chunks in grouped.items():
        assert [c.section for c in chunks] == ["지원대상", "선정기준", "지원내용", "신청방법"]   # 4항목 모두, 중복 없이
        assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_select_evidence_works_when_store_fails(monkeypatch):
    def _fail(sid):
        raise RuntimeError("no store")
    monkeypatch.setattr("src.index.vector_store.get_service_chunks", _fail)
    grouped = evidence.select_evidence(CASES["sr1"]["results"])
    assert grouped and all(len(cs) == 1 for cs in grouped.values())   # 검색 조각만으로 계속


def test_answer_question_with_real_b_search(indexed_sample, fake_conditions, monkeypatch):
    """B의 실제 hybrid.search·get_service_chunks(가짜 임베딩 색인) + C. LLM은 실패시켜 원문 대체 경로로 본다."""
    fake_conditions(CASES["sr1"])
    _fake_llm(monkeypatch, RuntimeError("no llm"))
    answer = generator.answer_question(QUESTION_WITH_RESULTS)
    assert not answer.no_evidence and 1 <= len(answer.recommendations) <= config.MAX_RECOMMENDATIONS
    names = [r.service_name for r in answer.recommendations]
    assert "청년월세 지원(부산)" not in names and "지난 청년 일자리 체험 사업" not in names   # 지역·기한 필터 (B)
    rent = next(r for r in answer.recommendations if r.service_name == "청년월세 지원(서울)")
    assert len(rent.citations) == 4 and "자료에 없음" not in rent.benefit   # 서비스 전체 항목이 근거로 들어감


# --- 답변 평가 실행 (eval.answer_eval.evaluate) -----------------------------------
def test_answer_eval_runner(fake_search, fake_conditions, monkeypatch):
    from eval import answer_eval

    def _chat(system, user):                    # 생성·채점 LLM을 프롬프트별로 고정
        if system == answer_eval.FAITHFULNESS_PROMPT:
            return {"verdicts": [{"id": i, "supported": True} for i in range(1, 50)]}
        if system == answer_eval.CORRECTNESS_PROMPT:
            return {"verdict": "충족"}
        raise RuntimeError("generator: 원문 대체 경로")
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", _chat)
    case = fake_search("sr1")
    fake_conditions(case)
    result = answer_eval.evaluate([{"id": "t1", "question": case["question"], "expected_answer": "주거 지원 안내",
                                    "expect_no_answer": False}])
    assert result["counts"] == {"total": 1, "scored": 1, "errors": 0}
    assert result["metrics"]["citation_accuracy"] == 1.0 and result["metrics"]["answer_correctness"] == 1.0
    assert "답변 평가" in answer_eval.format_report(result)


def test_reason_rejects_long_phrase_and_cross_type_age_match(monkeypatch):
    ev = evidence.select_evidence(CASES["sr1"]["results"])
    sid = list(ev)[0]                                             # 청년월세(서울): '만 19~34세 무주택 청년'
    _fake_llm(monkeypatch, {"services": [{"service_id": sid, "applicable": "확인 필요", "matches": [
        {"said": QUESTION_WITH_RESULTS, "condition": "무주택 청년"},     # 질문 문장 통째 -> 버림
        {"said": "취업준비생", "condition": "만 19~34세"},              # 나이 조건에 나이 아닌 구절 -> 버림
        {"said": "27살", "condition": "만 19~34세"},                    # 나이끼리 -> 인정
    ]}]})
    rec = generator.generate_answer(QUESTION_WITH_RESULTS, {sid: ev[sid]}).recommendations[0]
    assert "‘27살’" in rec.reason and "취업준비생" not in rec.reason and "받을 수 있는" not in rec.reason



# --- 필터 완화 안내 (B의 search_with_info.relaxed) ----------------------------------
def test_relaxed_notice_when_b_dropped_filters(fake_conditions, monkeypatch):
    from src.retrieve import hybrid
    fake_conditions(CASES["sr1"])                                      # age 27, 서울, 주거, 구직
    used = {"age": 27, "field": "주거", "situation": ["구직"], "today": "2026-10-19"}   # B가 region을 풀었다
    monkeypatch.setattr(hybrid, "search_with_info", lambda q, f, k=5, mode="hybrid_filter":
                        hybrid.SearchResult(list(CASES["sr1"]["results"]), [], used, True))
    _fake_llm(monkeypatch, RuntimeError("no llm"))
    answer = generator.answer_question(QUESTION_WITH_RESULTS)
    first = answer.text.splitlines()[0]
    assert generator.RELAXED_MARK in first and "지역(서울)" in first
    assert "나이" not in first and "분야" not in first                   # 분야는 필터가 아니라 안내 대상이 아님


def test_no_relaxed_notice_on_no_evidence(fake_conditions, monkeypatch):
    from src.retrieve import hybrid
    fake_conditions(CASES["sr1"])
    monkeypatch.setattr(hybrid, "search_with_info", lambda q, f, k=5, mode="hybrid_filter":
                        hybrid.SearchResult([], [], {"today": "2026-10-19"}, True))
    answer = generator.answer_question(QUESTION_WITH_RESULTS)
    assert answer.no_evidence and generator.RELAXED_MARK not in answer.text   # 근거 없음이면 완화 안내는 붙이지 않음


def test_field_is_not_a_filter_with_real_b_search(indexed_sample, fake_conditions, monkeypatch):
    """B 계약(PR #8): 관심 분야는 필터가 아니라 가산점. 월세 고민 + 구직 중이면 field가 '주거'여도
    일자리 분야 제도(구직촉진수당·서울 청년수당)가 후보에서 빠지지 않고, 완화 안내도 붙지 않는다."""
    fake_conditions(CASES["sr1"])                                      # field "주거", situation ["구직"]
    _fake_llm(monkeypatch, RuntimeError("no llm"))
    answer = generator.answer_question(QUESTION_WITH_RESULTS)
    names = [r.service_name for r in answer.recommendations]
    assert "청년월세 지원(서울)" in names
    assert {"구직촉진수당", "서울 청년수당"} & set(names)              # 다른 분야 제도도 후보에 남는다
    assert generator.RELAXED_MARK not in answer.text

