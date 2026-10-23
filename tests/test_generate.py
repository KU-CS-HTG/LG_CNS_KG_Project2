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
        "age": 27, "region": "서울", "field": "주거·자립", "situation": ["구직"]}   # 목록 밖 값·모르는 키는 버림
    assert condition_extractor._normalize_conditions({"region": "화성", "field": "여행", "age": True}) == {}


def test_normalize_conditions_maps_field_to_real_names():
    norm = condition_extractor._normalize_conditions
    assert norm({"field": "고용·창업"}) == {"field": "고용·창업"}               # 보조금24 서비스분야 원문
    assert norm({"field": "보건의료"}) == {"field": "보건·의료"}                 # 가운뎃점 없이 써도
    assert norm({"field": "창업"}) == {"field": "고용·창업"}                     # 한쪽 낱말만 써도
    assert norm({"field": "일자리"}) == {}                                      # 실제 분야에 없는 이름은 버림


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
    assert got.get("field") == "주거·자립" and "구직" in got.get("situation", [])
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
    assert SCOPE_NOTICE in answer.text                           # 자료 범위 안내 (README 1.1)


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


def test_faithfulness_judges_each_service_with_its_own_evidence(monkeypatch):
    """제도별로 따로, 그 제도의 근거만 넘겨 채점한다 (한 번에 채점하면 판정이 흔들림)."""
    from eval import answer_eval
    from src.common.schema import Recommendation
    recs = [Recommendation(service_name=n, agency="기관", reason="", benefit=f"{n} 지원", how_to_apply="")
            for n in ("가", "나")]
    calls = []
    def judge(system, user):
        calls.append(user)
        return {"verdicts": [{"id": 1, "supported": "근거가" in user}]}
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", judge)
    score = answer_eval.faithfulness(Answer(text="", recommendations=recs), "근거가\n근거나",
                                     {"가": "근거가", "나": "근거나"})
    assert len(calls) == 2 and "근거나" not in calls[0] and "근거가" not in calls[1]
    assert score == 0.5


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


def test_only_rescued_not_eligible_left_is_no_evidence(monkeypatch):
    # 질문 구절을 근거로 '해당 없음'이라 본 제도만 남으면 안내하지 않는다 (q33 '기초연금 신청'에 노인일자리 단독 카드)
    ev = evidence.select_evidence(CASES["sr1"]["results"])
    first, second = list(ev)[:2]
    said = QUESTION_WITH_RESULTS[:8]
    alone = {"service_id": first, "applicable": "해당 없음", "mismatch": {"said": said, "condition": "기초연금수급자"}}
    _fake_llm(monkeypatch, {"services": [alone]})
    assert generator.generate_answer(QUESTION_WITH_RESULTS, {first: ev[first]}).no_evidence
    # 다른 제도와 함께면 뒤로 보내 남긴다
    _fake_llm(monkeypatch, {"services": [alone, {"service_id": second, "applicable": "확인 필요"}]})
    answer = generator.generate_answer(QUESTION_WITH_RESULTS, {s: ev[s] for s in (first, second)})
    assert [r.service_name for r in answer.recommendations] == [ev[second][0].service_name, ev[first][0].service_name]


def test_unverified_not_eligible_goes_after_other_cards(monkeypatch):
    # 신분이 맞지 않는다는 판정('구직 등록한 장애인')은 코드로 확인할 수 없어 빼지 않지만 1번 카드가 되지 않게 뒤로 보낸다 (평가 q02)
    ev = evidence.select_evidence(CASES["sr1"]["results"])
    first, second = list(ev)[:2]
    _fake_llm(monkeypatch, {"services": [
        {"service_id": first, "applicable": "해당 없음", "mismatch": {"said": "졸업", "condition": "구직 등록한 장애인"}},
        {"service_id": second, "applicable": "확인 필요"}]})
    answer = generator.generate_answer(QUESTION_WITH_RESULTS, {s: ev[s] for s in (first, second)})
    assert [r.service_name for r in answer.recommendations] == [ev[second][0].service_name, ev[first][0].service_name]


def test_group_only_program_not_in_question_goes_later():
    assert generator._other_group("대학을 졸업했는데 취업을 못 했어요", "장애인 취업성공패키지")
    assert not generator._other_group("장애가 있는데 취업 지원 있나요?", "장애인 취업성공패키지")
    assert not generator._other_group("한부모 가정인데 양육비 지원 있나요", "한부모가족 아동양육비")
    assert generator._other_group("취업 준비 중이에요", "국가보훈대상자 취업수강료 지원")
    assert not generator._other_group("월세가 부담돼요", "주거급여 (맞춤형 급여)")


def test_new_enrollment_closed_is_shown_first_in_needs_check_and_goes_later(monkeypatch, sample_chunks):
    # 신청방법에 "'24년부터 신규지원 중단"이 있는 청년내일채움공제가 안내됐다 (q10 Faithfulness)
    from dataclasses import replace
    base = sample_chunks[0]
    def svc(sid, how):
        return [replace(base, service_id=sid, service_name=sid, section="신청방법", text=f"{sid}\n{how}")]
    ev = {"closed": svc("closed", "○ 신청방법: 고용24\n* '24년부터 신규지원 중단"), "open": svc("open", "온라인 신청")}
    monkeypatch.setattr(generator, "_application_period", lambda sid: "")
    assert generator._new_closed("closed", ev["closed"]) == "'24년부터 신규지원 중단"
    assert generator._new_closed("open", ev["open"]) == ""
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", lambda s, u: {"services": [
        {"service_id": sid, "applicable": "확인 필요"} for sid in ev if f"id={sid}]" in u]}
        if s == generator.SYSTEM_PROMPT else {})
    recs = generator._generate("적금 있나요?", ev, [], None).recommendations
    assert [r.service_name for r in recs] == ["open", "closed"]
    assert recs[1].needs_check[0] == "신규 신청 — '24년부터 신규지원 중단 (원문 확인)"


def test_help_phrase_is_not_paired_as_eligibility(sample_chunks):
    # '보증금을 빌려주거나 보증해 주는 제도' 조건이 대상 '무주택세대구성원'에 해당된다는 이유가 나왔다 (q22)
    from dataclasses import replace
    q = "월세 계약을 해야 하는데 보증금이 부족해요. 보증금을 빌려주거나 보증해 주는 제도가 있나요?"
    ch = [replace(sample_chunks[0], section="지원대상", text="장기전세\n무주택세대구성원")]
    pair = [{"said": "보증금을 빌려주거나 보증해 주는 제도", "condition": "무주택세대구성원"}]
    assert generator._grounded_matches(q, ch, pair)[0] == ["보증금을 빌려주거나 보증해 주는 제도"]   # 예전 동작
    assert generator._grounded_matches(q, ch, pair, ["보증금을 빌려주거나 보증해 주는 제도"]) == ([], [])


def test_change_notice_goes_later_unless_the_question_asks_about_changes():
    notice = "(변경·중지·연장 등 )부산광역시 평생함께 청년모두가 주거비 지원"
    assert generator._change_notice("부산 33살 직장인 주거 지원", notice)
    assert generator._change_notice("월세 지원 있나요", "청년월세 지원사업 변경신청(세종시 거주)")
    assert not generator._change_notice("청년월세 받다가 이사했어요", "청년월세 지원사업 변경신청(세종시 거주)")
    assert not generator._change_notice("외국인 주소 신고", "외국인 체류지변경신고 절차 간소화")   # 실제 서비스


def test_services_missing_from_llm_verdict_are_dropped(monkeypatch):
    ev = evidence.select_evidence(CASES["sr1"]["results"])
    _fake_llm(monkeypatch, {"services": []})                      # 판정은 왔지만 관련 있는 제도가 없음
    assert generator.generate_answer(QUESTION_WITH_RESULTS, ev).no_evidence is True


def test_not_eligible_needs_code_verified_mismatch():
    ok = generator._verified_mismatch
    q = "서울에 사는 27살이에요. 받을 수 있는 지원이 있나요?"
    assert ok(q, {"said": "27살", "condition": "만 35세~59세 여성"})            # 범위 밖: 해당 없음 인정
    assert ok(q, {"said": "27살", "condition": "24세 이하 청년"})
    assert not ok(q, {"said": "27살", "condition": "18세~45세"})              # 범위 안 (실제 데이터에서 빠졌던 경우)
    assert not ok(q, {"said": "27살", "condition": "만 19세~39세"})
    assert not ok(q, {"said": "27살", "condition": "청년"})                   # 연령 범위를 읽을 수 없음
    assert ok(q, {"said": "서울에 사는", "condition": "대전광역시에 주민등록"})   # 다른 시·도
    assert not ok(q, {"said": "서울에 사는", "condition": "서울특별시 거주 청년"})
    assert not ok(q, {"said": "서울에 사는", "condition": "무주택 청년"})        # 지역을 말하지 않는 구절
    assert not ok("청년도약계좌 가입 조건이 어떻게 되나요?",                    # 나이·거주지가 아닌 구절 (실제 데이터)
                  {"said": "청년도약계좌 가입 조건이 어떻게 되나요?", "condition": "만 15세~만 39세 이하"})


def test_missing_conditions_skip_what_evidence_text_states():
    from src.common.schema import Chunk
    def ch(text, agency="국토교통부"):
        return [Chunk("X#지원대상", "X", "가상 제도", "지원대상", text, {"field": "주거·자립", "agency": agency})]
    assert evidence._missing_conditions(ch("[가상 제도] 무주택 청년")) == []                        # 전국 제도: 지역 조건 없음
    assert evidence._missing_conditions(ch("[가상 제도] 무주택 청년", "충청남도 공주시")) == [
        "거주 지역 조건 (소관기관: 충청남도 공주시, 원문 확인)"]                                      # 지자체 제도인데 원문에 지역 없음
    assert evidence._missing_conditions(ch("[가상 제도] 서울 월세 거주 청년", "서울특별시")) == []   # 원문에 지역이 있음
    assert not any("연령" in n or "신청기한" in n                                                   # 연령·기한은 여기서 안 붙임
                   for n in evidence._missing_conditions(ch("[가상 제도] 청년", "경기도")))


def test_numeric_service_id_from_llm_still_matches(monkeypatch, sample_chunks):
    # 실제 보조금24 서비스ID는 숫자뿐이라 LLM이 JSON 숫자로 돌려준다 (실제 데이터에서 모든 제도가 빠졌던 문제)
    from dataclasses import replace
    chunks = [replace(c, service_id="568000000347") for c in sample_chunks if c.service_id == "SAMPLE001"]
    _fake_llm(monkeypatch, {"services": [{"service_id": 568000000347, "applicable": "확인 필요", "meets_need": True}]})
    answer = generator.generate_answer(QUESTION_WITH_RESULTS, {"568000000347": chunks})
    assert answer.no_evidence is False and len(answer.recommendations) == 1


def test_reason_uses_asked_help_when_no_condition_pairs(monkeypatch, sample_chunks):
    chunks = [c for c in sample_chunks if c.service_id == "SAMPLE001"]
    _fake_llm(monkeypatch, {"services": [{"service_id": "SAMPLE001", "applicable": "확인 필요", "meets_need": True}]})
    q = "월세가 부담돼요. 지원이 있나요?"                                     # 나이·지역 없이 원하는 도움만
    rec = generator._generate(q, {"SAMPLE001": chunks}, ["월세가 부담돼요"], ["월세가 부담돼요"]).recommendations[0]
    assert "‘월세가 부담돼요’에 도움이 될 수 있는" in rec.reason
    _fake_llm(monkeypatch, {"services": [{"service_id": "SAMPLE001", "applicable": "확인 필요"}]})   # meets_need 판정 없음
    rec = generator._generate(q, {"SAMPLE001": chunks}, [], ["월세가 부담돼요"]).recommendations[0]
    assert rec.reason == generator.GENERIC_REASON                           # 도와준다는 판정이 없으면 일반 문구


def test_application_period_comes_from_ingest_record(monkeypatch, sample_chunks, tmp_path):
    # 조각에는 날짜로 해석된 deadline만 있어 대부분 '상시 또는 정보 없음'이었다 -> A 저장소의 신청기한 원문을 쓴다
    chunks = [c for c in sample_chunks if c.service_id == "SAMPLE001"]
    _fake_llm(monkeypatch, {"services": [{"service_id": "SAMPLE001", "applicable": "확인 필요", "meets_need": True}]})
    db = tmp_path / "services.db"
    db.write_text("")
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(generator.ingest_store, "get_record",
                        lambda sid: {"payload": {"application_period": "연중 상시 신청(예산 소진 시 조기 마감될 수 있음)"}})
    rec = generator._generate("월세가 부담돼요", {"SAMPLE001": chunks}, [], None).recommendations[0]
    assert rec.how_to_apply.endswith(" / 신청기한: 연중 상시 신청(예산 소진 시 조기 마감될 수 있음)")

    def broken(sid):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(generator.ingest_store, "get_record", broken)          # 읽기 실패해도 답변은 나온다
    rec = generator._generate("월세가 부담돼요", {"SAMPLE001": chunks}, [], None).recommendations[0]
    assert "신청기한:" in rec.how_to_apply and "연중" not in rec.how_to_apply

    called = []
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "none.db")              # DB가 없으면 부르지 않는다 (새로 만들지 않게)
    monkeypatch.setattr(generator.ingest_store, "get_record", lambda sid: called.append(sid))
    generator._generate("월세가 부담돼요", {"SAMPLE001": chunks}, [], None)
    assert not called and not (tmp_path / "none.db").exists()


@pytest.mark.parametrize("piece, core", [
    # 체크리스트의 소득 줄이 문장 두 개를 이어 붙인 것처럼 길었다 -> 기준의 핵심만
    ("혼인일 기준 7년 이내 85㎡이하의 주택에서 임차하여 살고 있는 중위소득 180% 이내 가구", "중위소득 180% 이내 가구"),
    ("본인 연소득 6천만원 이하", "본인 연소득 6천만원 이하"),
    ("부부합산 연 소득 5천만원 이하인 자", "부부합산 연 소득 5천만원 이하인 자"),
    ("기준 중위 소득 180% 이하 무주택자인 신혼부부", "기준 중위 소득 180% 이하"),
    ("도시근로자 소득 70%(맞벌이 90%) 이하인 자", "도시근로자 소득 70%(맞벌이 90%) 이하인 자"),
    ("일반재산 1억 3천만원 이하", "일반재산 1억 3천만원 이하"),
    ("소득 기준은 은행 심사로 결정", ""),
])
def test_criterion_core(piece, core):
    assert generator._criterion_core(piece, "재산" if "재산" in piece else "소득") == core


@pytest.mark.parametrize("text, expired", [
    # 마감이 확실히 지난 기간 (deadline이 비어 B의 기한 필터를 통과하던 실제 원문들)
    ("2026년 상반기", True),
    ("(2026년 2학기) (1차) 2026년 5월 22일~6월 22일 (2차) 2026년 8월 12일~ 9월 9일", True),
    ("2026. 9. 1.(화) 09:00~9. 10.(목) 18:00까지", True),
    ("2026. 5. 6. ~ 6. 1. (사업종료)", True),
    ("2026년은 신규 대상자 미 모집", True),
    ("25년 신청기한 : 2025.2.1. ~ 2025.4.30.", True),
    ("2025/1/10~2025/12/12(또는 예산 소진 시까지)", True),          # 끝 날짜가 있는 마감
    ("2025.03.01.~10.15. (매월 20일 ~ 익월 15일 접수, 모집인원 초과 시 선착순 마감)", True),
    ("(상반기)2026. 3 ~ 4월 / (하반기)2026. 8 ~ 9월", True),         # '(하반기)'는 소제목
    # 거르지 않는 기간
    ("2026.01.26~2026.12.18", False),
    ("20260201~20261215", False),
    ("’26년 1월 ~ 예산 소진 시까지", False),                        # 열린 끝
    ("2024. 5. ~", False),
    ("상반기 : 2026-01-02~2026-02-13 / 하반기 : 미정", False),
    ("2026.4.~ 12. 매월 1~10일 신청 ※ 선착순 접수로 예산 소진 시 조기 마감", False),
    ("연1회 신청(8월말~9월초)", False),                              # 연도 없음 (해마다 반복될 수 있음)
    ("상시신청", False),
    ("", False),
])
def test_period_expired(text, expired):
    from datetime import date
    assert generator._period_expired(text, date(2026, 10, 21)) is expired


def test_expired_services_are_dropped_and_upcoming_marked(monkeypatch, sample_chunks, tmp_path):
    from datetime import date
    periods = {"SAMPLE001": "2026년 상반기", "SAMPLE004": "2099.11.01~2099.11.30"}
    db = tmp_path / "services.db"
    db.write_text("")
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(generator.ingest_store, "get_record",
                        lambda sid: {"payload": {"application_period": periods.get(sid, "")}})
    periods["SAMPLE005"] = "(2020년 2학기) (1차) 2020년 5월 22일~6월 22일"           # 학기마다 반복 (국가장학금)
    kept = generator._drop_expired(sample_chunks, date(2026, 10, 21))
    assert not any(c.service_id == "SAMPLE001" for c in kept)                     # 마감 지남 -> 후보에서 제외
    assert any(c.service_id == "SAMPLE004" for c in kept)                         # 시행 예정 -> 남김
    assert any(c.service_id == "SAMPLE005" for c in kept)                         # 이번 회차만 끝난 반복 제도 -> 남김
    recurring = [c for c in sample_chunks if c.service_id == "SAMPLE005"]
    _fake_llm(monkeypatch, {"services": [{"service_id": "SAMPLE005", "applicable": "확인 필요", "meets_need": True}]})
    rec = generator._generate("구직 중이에요", {"SAMPLE005": recurring}, [], None).recommendations[0]
    assert f" / 신청기한: {generator.CLOSED_RECURRING_MARK} · (2020년 2학기)" in rec.how_to_apply
    chunks = [c for c in sample_chunks if c.service_id == "SAMPLE004"]
    _fake_llm(monkeypatch, {"services": [{"service_id": "SAMPLE004", "applicable": "확인 필요", "meets_need": True}]})
    rec = generator._generate("구직 중이에요", {"SAMPLE004": chunks}, [], None).recommendations[0]
    assert rec.how_to_apply.endswith(f" / 신청기한: {generator.UPCOMING_MARK} · 2099.11.01~2099.11.30")


def test_each_service_is_judged_alone_and_missing_judgement_is_retried(monkeypatch, sample_chunks):
    # 5개를 한 번에 물으면 뒤쪽 제도를 '정보 부족 -> 관련 없음'으로 넘겼다 (평가 q29) -> 제도마다 따로 묻는다.
    # 같은 제도를 여러 번 적으면(나중 것이 '관련 없음') 첫 판정을 쓰고, 판정이 빠진 제도는 한 번 더 묻는다
    import threading
    ev = evidence.select_evidence([c for c in sample_chunks if c.service_id in ("SAMPLE001", "SAMPLE004")])
    a, b = list(ev)
    dup_a = {"situation": "구직", "follow_up": "",
             "services": [{"service_id": a, "applicable": "확인 필요", "meets_need": True},
                          {"service_id": a, "applicable": "관련 없음", "meets_need": False}]}
    ok_b = {"follow_up": "소득을 알려 주세요.", "services": [{"service_id": b, "applicable": "확인 필요", "meets_need": True}]}
    prompts, lock = [], threading.Lock()

    def chat(system, user):
        if system != generator.SYSTEM_PROMPT:
            return {}
        with lock:
            prompts.append(user)
            first_b = sum(f"id={b}" in p for p in prompts) == 1
        if f"id={a}" in user:
            return dup_a
        return {"services": []} if first_b else ok_b          # b는 처음에 판정이 빠졌다
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", chat)
    answer = generator._generate("서울에서 받을 수 있는 지원 있나요?", ev, [], None)   # 지역 질문: 순서는 검색 순서
    assert all((f"id={a}" in p) != (f"id={b}" in p) for p in prompts)   # 프롬프트마다 제도 하나
    assert len(prompts) == 3                                            # a 1번, b 2번 (빠져서 다시)
    assert [r.service_name for r in answer.recommendations] == [ev[a][0].service_name, ev[b][0].service_name]
    assert "소득을 알려 주세요." in answer.text                         # 비어 있지 않은 첫 follow_up


def test_more_candidates_are_judged_but_only_five_are_shown(monkeypatch, sample_chunks):
    import re
    from dataclasses import replace
    base = next(c for c in sample_chunks if c.section == "지원대상")      # 체크리스트는 지원대상·선정기준에서 뽑는다
    ev = {f"S{i}": [replace(base, service_id=f"S{i}", service_name=f"제도{i}")] for i in range(8)}
    judged, checked = [], []

    def chat(system, user):
        sid = re.search(r"id=(S\d)", user).group(1)
        (judged if system == generator.SYSTEM_PROMPT else checked).append(sid)
        if system == generator.SYSTEM_PROMPT:
            return {"services": [{"service_id": sid, "applicable": "관련 없음" if sid == "S0" else "확인 필요"}]}
        return {"services": [{"id": sid, "conditions": [{"item": "연령", "criterion": ""}]}]}
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", chat)
    recs = generator._generate("지원 있나요?", ev, [], None).recommendations
    assert sorted(judged) == sorted(ev) and sorted(checked) == sorted(ev)   # 8개 모두 따로 판정·체크리스트
    assert [r.service_name for r in recs] == ["제도1", "제도2", "제도3", "제도4", "제도5"]   # S0이 빠진 자리를 6번째가 채움

    # 상위 5개가 모두 관련 없으면 뒤 후보만 통과해도 근거 없음 (근거 없음 질문에 뒤 후보가 안내되던 문제, q33)
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", lambda s, u: {"services": [{
        "service_id": re.search(r"id=(S\d)", u).group(1),
        "applicable": "확인 필요" if "id=S7" in u else "관련 없음"}]} if s == generator.SYSTEM_PROMPT else {})
    assert generator._generate("지원 있나요?", ev, [], None).no_evidence is True


def test_card_order_puts_likely_and_national_first_for_questions_without_region(monkeypatch, sample_chunks):
    # 판정 후보 10개 중 앞 5개만 나가 검색 순서로만 정하면 판정 '가능'인 정답·전국 제도가 잘렸다 (q09·q12)
    from dataclasses import replace
    base = sample_chunks[0]
    def svc(sid, agency):
        return [replace(base, service_id=sid, service_name=sid, metadata={**base.metadata, "agency": agency, "region": None})]
    ev = {"local": svc("local", "재단법인강원인재원"), "unclear": svc("unclear", "한국장학재단"),
          "likely": svc("likely", "교육부"), "jeonju": svc("jeonju", "전주시복지재단전주사람")}
    verdict = {"local": "확인 필요", "unclear": "확인 필요", "likely": "가능", "jeonju": "가능"}
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", lambda s, u: {"services": [
        {"service_id": sid, "applicable": verdict[sid]} for sid in verdict if f"id={sid}]" in u]}
        if s == generator.SYSTEM_PROMPT else {})
    names = lambda q: [r.service_name for r in generator._generate(q, ev, [], None).recommendations]  # noqa: E731
    # '재단법인강원인재원'은 이름으로 지역을 알 수 없어 전국 쪽에 남는다 (저장된 지역 값이 없을 때)
    assert names("학자금 이자 지원 있나요?") == ["likely", "local", "unclear", "jeonju"]
    assert names("전북에 사는데 학자금 이자 지원 있나요?") == ["likely", "jeonju", "local", "unclear"]   # 지역 질문은 지자체를 뒤로 보내지 않음


def test_single_service_judgement_with_wrong_id_is_kept(monkeypatch, sample_chunks):
    # 제도 하나만 물었는데 id를 바꿔 적어도(숫자 id) 판정이 하나뿐이면 그 제도의 판정이다
    ev = evidence.select_evidence([c for c in sample_chunks if c.service_id == "SAMPLE001"])
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", lambda s, u: {
        "services": [{"service_id": "12345", "applicable": "확인 필요", "meets_need": True}]}
        if s == generator.SYSTEM_PROMPT else {})
    assert len(generator._generate("지원 있나요?", ev, [], None).recommendations) == 1
    monkeypatch.setattr("src.generate.condition_extractor._chat_json",
                        lambda s, u: (_ for _ in ()).throw(TimeoutError()) if s == generator.SYSTEM_PROMPT else {})
    assert generator._judge("지원 있나요?", ev, []) == {}                 # 모두 실패하면 판정 없음


def test_district_question_drops_other_districts_in_same_city(sample_chunks):
    # 시·도까지만 비교하면 '서울 강남구' 질문에 동대문구 주민 대상 제도가 나왔다
    from dataclasses import replace
    def at(sid, agency):
        return replace(sample_chunks[0], service_id=sid, metadata={**sample_chunks[0].metadata, "agency": agency})
    chunks = [at("G", "서울특별시 강남구"), at("D", "서울특별시 동대문구"), at("S", "서울특별시"), at("N", "보건복지부")]
    assert generator._find_districts("서울 강남구에 사는 30살이에요", "서울") == {"강남구"}
    assert generator._find_districts("3인 가구인데 친구랑 수시로 상담해요") == set()      # 구·시로 끝나는 일반 낱말
    assert generator._find_districts("광주에 사는데 광주시청 근처예요", "광주") == set()    # 경기 광주시는 광주광역시가 아님
    assert generator._find_districts("대전 서구에 살아요", "대전") == {"서구"}
    kept = [c.service_id for c in generator._drop_other_districts(chunks, {"강남구"})]
    assert kept == ["G", "S", "N"]                                   # 다른 구만 빠지고 시 전체·전국 제도는 남는다
    assert generator._drop_other_districts(chunks, set()) == chunks  # 구를 말하지 않으면 그대로
    only_other = [at("D", "서울특별시 동대문구")]
    assert generator._drop_other_districts(only_other, {"강남구"}) == only_other   # 다 빠지면 원래 후보 (소프트 필터)


def test_agency_region_from_district_name_inside_foundation_name():
    # 지자체 산하 기관은 이름에 시·도가 없어 전국 제도로 보였다 (서울 질문에 의왕시·시흥시 청소년 상담, 평가 q36)
    assert generator._agency_region("재단법인의왕시청소년재단") == "경기"
    assert generator._agency_region("동대문구시설관리공단") == "서울"
    assert generator._agency_region("재단법인광주시문화재단") == "경기"       # 경기 광주시 (광주광역시 아님)
    assert generator._agency_region("충청북도 충주시") == "충북"              # 원래 방식 그대로
    assert generator._agency_region("중구시설관리공단") is None               # 여러 시·도에 있는 이름
    assert generator._agency_region("한국남동발전") is None                   # 끝 글자 없는 '남동'은 보지 않는다
    assert generator._agency_region("재단법인용산복지재단") is None           # '용산구'가 아니라 '용산'
    assert generator._agency_region("보건복지부") is None


def test_district_programs_must_match_interest_field(sample_chunks):
    # 검색어의 '강남구'가 주제와 상관없는 강남구 제도를 끌어올렸다 (취업준비생 질문에 출산양육지원금)
    from dataclasses import replace
    def at(sid, agency, field):
        return replace(sample_chunks[0], service_id=sid,
                       metadata={**sample_chunks[0].metadata, "agency": agency, "field": field})
    chunks = [at("birth", "서울특별시 강남구", "임신·출산"), at("job", "서울특별시 강남구", "고용·창업"),
              at("city", "서울특별시", "생활안정"), at("nat", "고용노동부", "고용·창업")]
    kept = [c.service_id for c in generator._drop_other_districts(chunks, {"강남구"}, "고용·창업")]
    assert kept == ["job", "city", "nat"]          # 구 제도만 분야로 거르고, 시 전체·전국 제도는 그대로
    assert [c.service_id for c in generator._drop_other_districts(chunks, {"강남구"})] == [
        "birth", "job", "city", "nat"]             # 관심 분야가 없으면 거르지 않는다 ('곧 아이가 태어나요')


def test_district_program_with_other_field_kept_when_it_mentions_the_help(monkeypatch, sample_chunks):
    # 분야 값이 '생활안정'인 동대문구 '저소득층 중개보수 지원'(월세 환산보증금)이 '월세 지원' 질문에서 빠졌다
    from dataclasses import replace
    def at(sid, field, text):
        return replace(sample_chunks[0], service_id=sid, text=text,
                       metadata={**sample_chunks[0].metadata, "agency": "서울특별시 동대문구", "field": field})
    broker = at("broker", "생활안정", "저소득층 중개보수 지원 (지원대상) 기초생활수급자")
    safety = at("safety", "안전", "1인가구 안심장비 지원사업 현관문 이중잠금장치")
    full = {"broker": [replace(broker, text="주택임대차 중개보수 지원, 월세 환산보증금 1억원 이하")]}
    monkeypatch.setattr(generator.evidence_mod.vector_store, "get_service_chunks", lambda sid: full.get(sid, []))
    kept = generator._drop_other_districts([broker, safety], {"동대문구"}, "주거·자립", ["월세 지원"])
    assert [c.service_id for c in kept] == ["broker"]   # 원문 전체에 '월세'가 있어 남고, '지원'만 맞는 제도는 빠진다


def test_llm_says_missing_but_raw_section_exists_uses_raw():
    # 근거에 신청방법 '방문, 인터넷'이 있는데 LLM이 '신청방법은 자료에 없음'이라고 썼다 (평가 q13)
    assert generator._llm_or_raw("신청방법은 자료에 없음.", "방문, 인터넷") == "방문, 인터넷"
    assert generator._llm_or_raw("관련 정보가 없습니다", "온라인 신청") == "온라인 신청"
    assert generator._llm_or_raw("복지로 온라인 신청", "방문, 인터넷") == "복지로 온라인 신청"   # 요약이 있으면 요약
    assert generator._llm_or_raw("", "방문") == "방문"
    assert generator._llm_or_raw("자료에 없음", None) == "자료에 없음"                         # 원문도 없으면 그대로
    assert generator._llm_or_raw(None, None) == generator.NOT_IN_EVIDENCE


def test_rent_question_drops_purchase_only_program():
    # '보증금 대출' 질문에 '구매용도만 가능'한 내집마련 디딤돌 대출이 안내됐다 (평가 q06)
    from src.common.schema import Chunk
    def ch(text):
        return [Chunk("X#지원내용", "X", "가상 제도", "지원내용", text, {})]
    buy = ch("주택을 구입하는 경우 대출 (구매용도만 가능)")
    rent = ch("전세 보증금 대출 이자 지원")
    public_rent = ch("공공사업자가 다가구주택 등을 매입하여 임대 (분양전환 가능)")
    assert generator._tenure_mismatch(["보증금 대출", "이자 지원"], buy)
    assert not generator._tenure_mismatch(["보증금 대출"], rent)
    assert generator._tenure_mismatch(["내 집 마련 대출"], rent)                   # 반대 방향
    assert not generator._tenure_mismatch(["보증금 대출"], public_rent)            # 임대·분양 둘 다 있으면 판단하지 않음
    assert not generator._tenure_mismatch(["신혼집 지원"], buy)                     # 어느 쪽인지 말하지 않음
    assert not generator._tenure_mismatch(None, buy)


def test_all_dropped_judgement_is_asked_once_more(monkeypatch, sample_chunks):
    # 같은 후보로도 판정이 흔들려 모두 '관련 없음'이 나오기도 했다 (q07: 6번 중 2번 근거 없음) -> 한 번 더 받는다
    ev = evidence.select_evidence([c for c in sample_chunks if c.service_id == "SAMPLE001"])
    sid = next(iter(ev))
    none_ = {"services": [{"service_id": sid, "applicable": "관련 없음", "meets_need": False}]}
    ok = {"services": [{"service_id": sid, "applicable": "확인 필요", "meets_need": True}]}
    replies = []

    def chat(system, user):
        if system != generator.SYSTEM_PROMPT:
            return {}
        replies.append(1)
        return none_ if len(replies) == 1 else ok
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", chat)
    answer = generator._generate("지원 있나요?", ev, ["월세"], ["월세"])
    assert len(replies) == 2 and len(answer.recommendations) == 1   # 두 번째 판정으로 안내

    replies.clear()
    monkeypatch.setattr("src.generate.condition_extractor._chat_json",
                        lambda s, u: (replies.append(1) or none_) if s == generator.SYSTEM_PROMPT else {})
    assert generator._generate("지원 있나요?", ev, ["월세"], ["월세"]).no_evidence is True
    assert len(replies) == 2                                        # 두 번 다 관련 없으면 근거 없음 (더 묻지 않음)

    replies.clear()                                                 # 이름으로 물은 제도가 후보에 없으면 다시 묻지 않는다
    assert generator._generate("청년희망적금 되나요?", ev, ["청년희망적금"], ["청년희망적금"]).no_evidence is True
    assert len(replies) == 1


def test_region_question_keeps_slots_for_national_programs(monkeypatch, sample_chunks):
    # 지역을 말하면 그 지역 제도가 상위 5개를 다 채워 전국 제도(출산급여 등)가 빠졌다 -> 지역 최대 3, 전국 최소 2
    from dataclasses import replace
    local = [replace(c, service_id=f"L{i}", metadata={**c.metadata, "agency": "대전광역시"})
             for i, c in enumerate(sample_chunks[:5])]
    national = [replace(c, service_id=f"N{i}", metadata={**c.metadata, "agency": "보건복지부"})
                for i, c in enumerate(sample_chunks[:3])]
    mixed = generator._mix_national(local, national)
    first5 = list(dict.fromkeys(c.service_id for c in mixed))[:5]
    assert first5 == ["L0", "L1", "L2", "N0", "N1"]
    only_local = list(dict.fromkeys(c.service_id for c in generator._mix_national(local, [])))[:5]
    assert only_local == ["L0", "L1", "L2", "L3", "L4"]          # 전국 후보가 없으면 지역 제도로 채운다


def test_judge_candidates_are_filled_without_changing_top_order(fake_conditions, monkeypatch, sample_chunks):
    # 처음부터 후보 수를 키우면 B의 순위가 바뀌어(q03) 처음 검색(k=5)의 순서는 그대로 두고, 판정 후보 10개의
    # 나머지와 기한 지난 제도가 빠진 자리를 더 큰 검색으로 뒤에 채운다
    from src.retrieve import hybrid
    by_sid = {}
    for c in sample_chunks:
        by_sid.setdefault(c.service_id, c)
    first = list(by_sid)
    big = list(reversed(first))                                # 큰 검색은 순위가 다르다고 가정
    calls = []

    def search(query, filters, k=5, mode="hybrid_filter"):
        calls.append(k)
        return hybrid.SearchResult([by_sid[s] for s in (first if k == 5 else big)[:k]], [], filters, False)
    monkeypatch.setattr(hybrid, "search_with_info", search)
    fake_conditions({"conditions": {}})
    _fake_llm(monkeypatch, {})
    expired = first[1]
    monkeypatch.setattr(generator, "_application_period", lambda sid: "2020년 상반기" if sid == expired else "")
    _, selected = generator._answer_with_evidence("받을 수 있는 지원")
    assert calls == [5, 15]
    top = [s for s in first[:5] if s != expired]
    rest = [s for s in big if s not in first[:5] and s != expired]
    assert list(selected) == (top + rest)[:generator.JUDGE_CANDIDATES]   # 처음 순서 유지, 뒤는 큰 검색 순서


def _fake_two_calls(monkeypatch, answer_out: dict, checklist_out: dict):
    """답변 생성과 자격 체크리스트 추출이 같은 _chat_json을 쓰므로 시스템 프롬프트로 나눠 돌려준다."""
    monkeypatch.setattr("src.generate.condition_extractor._chat_json",
                        lambda s, u: checklist_out if s == generator.CHECKLIST_PROMPT else answer_out)


def test_checklist_comes_from_separate_extraction_and_is_grounded(monkeypatch, sample_chunks):
    # 답변 생성의 needs_check는 소득 한두 줄뿐이라 무주택 같은 핵심 자격이 빠졌다 (청년월세·버팀목)
    chunks = [c for c in sample_chunks if c.service_id == "SAMPLE001"]   # '서울에 거주하며 월세로 사는 만 19~34세 무주택 청년'
    _fake_two_calls(monkeypatch,
                    {"services": [{"service_id": "SAMPLE001", "applicable": "확인 필요", "meets_need": True,
                                   "needs_check": [{"item": "소득", "criterion": ""}]}]},
                    {"services": [{"id": "SAMPLE001", "conditions": [
                        {"item": "주택", "criterion": "무주택"},
                        {"item": "연령", "criterion": "만 19~34세"},
                        {"item": "자녀", "criterion": "자녀 3명 이상"}]}]})     # 원문에 없는 조건 -> 버림
    rec = generator._generate("월세가 부담돼요", {"SAMPLE001": chunks}, [], None).recommendations[0]
    assert rec.needs_check[:2] == ["주택 — 무주택", "연령 — 만 19~34세"]      # 이름은 추출 결과 그대로
    assert not any("자녀" in n for n in rec.needs_check)
    # 27살이라고 말해 코드로 나이 충족이 확인되면 연령은 체크할 필요가 없다
    rec = generator._generate("27살인데 월세가 부담돼요", {"SAMPLE001": chunks}, [], None).recommendations[0]
    assert "주택 — 무주택" in rec.needs_check and not any(n.startswith("연령") for n in rec.needs_check)


def test_checklist_falls_back_to_answer_needs_when_extraction_fails(monkeypatch, sample_chunks):
    chunks = [c for c in sample_chunks if c.service_id == "SAMPLE001"]
    _fake_two_calls(monkeypatch,
                    {"services": [{"service_id": "SAMPLE001", "applicable": "확인 필요", "meets_need": True,
                                   "needs_check": [{"item": "주택", "criterion": "무주택"}]}]},
                    {})                                                          # 추출 실패·형식 오류
    rec = generator._generate("월세가 부담돼요", {"SAMPLE001": chunks}, [], None).recommendations[0]
    assert "주택 조건 — 무주택" in rec.needs_check


def test_named_program_question_drops_other_programs(monkeypatch, sample_chunks):
    # '청년희망적금' 질문에 이름이 비슷한 청년미래적금을 안내하던 문제 (평가 q30)
    from dataclasses import replace
    other = [replace(c, service_name="청년미래적금") for c in sample_chunks if c.service_id == "SAMPLE001"]
    _fake_llm(monkeypatch, {"services": [{"service_id": "SAMPLE001", "applicable": "확인 필요", "meets_need": True}]})
    q = "청년희망적금에 지금 가입할 수 있나요?"
    assert generator._generate(q, {"SAMPLE001": other}, ["청년희망적금"], ["청년희망적금"]).no_evidence
    same = [replace(c, service_name="대구 청년희망적금") for c in other]
    assert not generator._generate(q, {"SAMPLE001": same}, ["청년희망적금"], ["청년희망적금"]).no_evidence
    assert generator._named_programs(["청년희망적금", "청년 적금", "청년적금", "장학금", "주거비"]) == ["청년희망적금"]
    assert generator._named_programs(["청년도약계좌 가입 조건"]) == ["청년도약계좌"]          # 뒤에 말이 붙은 구절 (q25)
    # 가운뎃점으로 묶인 이름은 각각으로 인정 ('근로장려금' 질문에 1위 '근로·자녀장려금'을 빼던 문제)
    eitc = [replace(c, service_name="근로·자녀장려금") for c in other]
    for asked in ("근로장려금", "자녀장려금"):
        assert not generator._generate(f"{asked}은 어떻게 신청하나요?", {"SAMPLE001": eitc}, [asked], [asked]).no_evidence


def test_need_claim_is_rewritten_for_the_judge():
    from eval import answer_eval
    reason = "말씀하신 ‘보증금 대출’에 도움이 될 수 있는 제도예요. 자격은 아래 조건을 확인해 주세요."
    assert answer_eval._need_claim(reason) == "찾는 도움: ‘보증금 대출’ — 이 제도의 지원내용이 이 도움을 직접 준다."
    assert "찾는 도움:" in answer_eval.FAITHFULNESS_PROMPT


def test_reason_names_only_the_needs_the_service_meets(monkeypatch, sample_chunks):
    # 처지(구직)만 맞은 제도에 질문 구절('월세가 부담되고')로 이유를 붙이면 틀린 이유가 된다 (평가 q01)
    from dataclasses import replace
    rent = [c for c in sample_chunks if c.service_id == "SAMPLE001"]                    # 원문에 '월세'가 있는 제도
    job = [replace(c, text=c.text.replace("월세", "구직활동")) for c in rent]             # 원문에 '월세'가 없는 제도
    q = "월세가 부담되고 취업을 준비하고 있어요"
    needs = ["월세가 부담되고", "구직(일자리를 찾고 있거나 실직·퇴사한 상태)"]
    _fake_llm(monkeypatch, {"services": [{"service_id": "SAMPLE001", "applicable": "확인 필요", "meets_need": True}]})
    def reason(chunks):
        return generator._generate(q, {"SAMPLE001": chunks}, needs, ["월세가 부담되고"]).recommendations[0].reason
    assert "‘월세가 부담되고’에 도움" in reason(rent)
    assert "‘구직 상황’에 도움" in reason(job) and "월세" not in reason(job)


def test_needs_check_shows_criterion_quoted_from_evidence(monkeypatch, sample_chunks):
    chunks = [c for c in sample_chunks if c.service_id == "SAMPLE001"]   # 지원대상: '만 19~34세 무주택 청년' 등
    body = " ".join(c.text for c in chunks)
    quoted = "만 19~34세" if "만 19~34세" in body else body.split("]")[-1].strip()[:15]
    _fake_llm(monkeypatch, {"services": [{"service_id": "SAMPLE001", "applicable": "확인 필요", "needs_check": [
        {"item": "연령", "criterion": quoted},                      # 근거 원문에 있음 -> 기준까지 표시
        {"item": "소득", "criterion": "기준 중위소득 999% 이하"},     # 근거에 없는 수치 -> 이름 + 원문 확인 안내
        "병역 의무"]}]})                                             # 예전 형식(문자열). 원문에 없는 조건 -> 뺌
    rec = generator.generate_answer(QUESTION_WITH_RESULTS, {"SAMPLE001": chunks}).recommendations[0]
    assert f"연령 — {quoted}" in rec.needs_check
    assert f"소득 {generator.NO_CRITERION}" in rec.needs_check and not any("999%" in n for n in rec.needs_check)
    assert not any("병역" in n for n in rec.needs_check)


def test_reason_pairs_age_and_region_by_code_when_llm_said_is_too_long(monkeypatch):
    from src.common.schema import Chunk
    ch = [Chunk("X#지원대상", "X", "가상 월세", "지원대상", "[가상 월세] 서울 월세 거주 19세 ~ 39세 이하 청년 1인 가구", {}),
          Chunk("X#신청방법", "X", "가상 월세", "신청방법", "[가상 월세] 부산 센터 방문", {})]
    q = "서울에서 혼자 사는 26살인데 월세 내고 나면 생활비가 빠듯해요"
    _fake_llm(monkeypatch, {"services": [{"service_id": "X", "applicable": "확인 필요",
                                          "matches": [{"said": q, "condition": "서울 월세 거주"}]}]})   # 질문 통째 -> 버림
    rec = generator.generate_answer(q, {"X": ch}).recommendations[0]
    assert "‘서울에서’" in rec.reason and "‘26살’" in rec.reason                # 코드가 직접 짝지음
    assert "서울 월세 거주 19세 ~ 39세 이하 청년 1인 가구" in rec.reason
    assert generator._code_matches("부산 사는 42살이에요", ch) == ([], [])        # 범위 밖 나이, 대상에 없는 지역(신청방법만)은 짝 없음


def test_drop_other_region_local_services():
    from src.common.schema import Chunk
    def ch(sid, agency):
        return Chunk(f"{sid}#지원대상", sid, sid, "지원대상", f"[{sid}] 청년 면접비", {"agency": agency})
    chunks = [ch("A", "충청북도 충주시"), ch("B", "대구광역시"), ch("C", "고용노동부"), ch("D", "대구광역시 수성구")]
    kept, relaxed = generator._drop_other_regions(chunks, "대구")
    assert [c.service_id for c in kept] == ["B", "C", "D"] and relaxed is False     # 다른 지역 지자체만 빠짐, 전국 제도는 남음
    kept, relaxed = generator._drop_other_regions([ch("A", "충청북도 충주시")], "대구")
    assert [c.service_id for c in kept] == ["A"] and relaxed is True                # 다 빠지면 그대로 두고 완화로 알림
    assert generator._drop_other_regions(chunks, None) == (chunks, False)            # 지역을 안 말하면 그대로


def test_criterion_label_follows_criterion_text():
    lab = generator._criterion_label
    assert lab("임차 보증금 8천만원 이하, 월세 60만원 이하 건물 월세 거주", "거주지") == "주택 조건"
    assert lab("기준 중위소득 150% 이하 무주택자", "소득") == "소득"
    assert lab("일반재산 1억 3천만원 이하, 자동차 2,500만원 미만", "재산") == "재산"
    assert lab("도내 고교출신 도내 대학생 재학생", "학적") == "거주지"
    assert lab("대학 재학 중", "학적") == "학적"                                 # 맞는 분류가 없으면 LLM 이름


def test_income_criterion_found_by_code_even_if_one_mention_has_no_number():
    sections = {"지원대상": "(소득기준) (미혼) 본인 연소득 6천만원 이하, (기혼) 부부합산 연소득 8천만원 이하 "
                            "· 정확한 연소득 기준은 대출신청시 은행의 소득심사로 결정함"}        # 인천 임차보증금 이자 지원 원문
    assert generator._vague_criteria(sections) == ["소득 — 본인 연소득 6천만원 이하, 부부합산 연소득 8천만원 이하"]
    assert generator._vague_criteria({"선정기준": "소득이 기준 이하인 청년"}) == ["소득 기준 (자료에 기준 금액 없음)"]


def test_reason_pairs_only_with_target_conditions_not_benefit(monkeypatch):
    from src.common.schema import Chunk
    ch = [Chunk("X#지원대상", "X", "가상", "지원대상", "[가상] 인천광역시에 주민등록이 되어 있는 무주택 청년", {}),
          Chunk("X#지원내용", "X", "가상", "지원내용", "[가상] 대출금에 대한 이자 지원", {})]
    q = "인천에 사는데 이자를 지원받고 싶어요"
    said, conds = generator._grounded_matches(q, ch, [{"said": "이자를 지원받고 싶어", "condition": "대출금에 대한 이자 지원"}])
    assert (said, conds) == ([], [])                                           # 원하는 도움 ↔ 지원내용은 조건 짝이 아님


def test_needs_check_criterion_ignores_footnote_marks():
    from src.common.schema import Chunk
    ch = [Chunk("X#선정기준", "X", "가상 제도", "선정기준",
                "[가상 제도] ㅇ (소득) 청년 원가구*의 소득이 기준 중위소득 100% 이하", {})]
    got = generator._needs_with_criteria([{"item": "소득", "criterion": "청년 원가구의 소득이 기준 중위소득 100% 이하"}], ch)
    assert got == ["소득 — 청년 원가구의 소득이 기준 중위소득 100% 이하"]     # 원문의 '*'를 빼고 옮겨도 인정
    assert generator._needs_with_criteria([{"item": "소득", "criterion": "기준 중위소득 120% 이하"}], ch) == [
        f"소득 {generator.NO_CRITERION}"]                                    # 지어낸 수치는 빼고 원문 확인 안내
    assert generator._needs_with_criteria([{"item": "병역", "criterion": ""}], ch) == []   # 원문에 없는 조건은 뺌


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
    shown = case["results"][0].service_id                   # 검색 1위 제도 (원문 대체 경로라 그대로 안내됨)
    result = answer_eval.evaluate([{"id": "t1", "question": case["question"], "expected_answer": "주거 지원 안내",
                                    "expect_no_answer": False, "relevant_service_ids": [shown, "OUT_OF_SCOPE"]}],
                                  indexed=lambda sid: sid != "OUT_OF_SCOPE")
    assert result["counts"] == {"total": 1, "scored": 1, "errors": 0, "service_labeled": 1, "unreachable_ids": 1}
    assert result["metrics"]["citation_accuracy"] == 1.0 and result["metrics"]["answer_correctness"] == 1.0
    assert result["metrics"]["service_hit"] == 1.0 and result["metrics"]["service_recall"] == 1.0   # 색인 밖 정답은 분모에서 뺌
    assert "정답 제도 안내" in answer_eval.format_report(result)


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



# --- 원하는 도움을 주지 않는 제도 거르기 (needs · meets_need) --------------------------
def test_needs_from_conditions():
    needs = generator._needs_from
    assert needs({"help": ["관람비"], "field": "문화"}) == ["관람비"]
    got = needs({"help": ["월세가 부담돼요"], "field": "주거", "situation": ["구직"]})
    assert got[0] == "월세가 부담돼요" and got[1].startswith("구직")         # 월세 고민 + 구직 중: 둘 다
    assert needs({"field": "일자리", "situation": ["구직"]}) == []            # 직접 말한 도움 없음(처지로 짐작한 분야): 거르지 않음
    assert needs({"age": 27, "region": "서울"}) == []                         # 넓게 묻는 질문


def test_help_must_be_quoted_from_question():
    norm = condition_extractor._normalize_conditions
    q = "대학 등록금이 부담돼요. 받을 수 있는 장학금이 있나요?"
    assert norm({"help": ["등록금", "장학금", "생활비"]}, question=q)["help"] == ["등록금", "장학금"]   # 질문에 없는 '생활비'는 버림
    assert "help" not in norm({"help": []}, question=q)
    q2 = "청년이 해외 주식 투자에서 손실을 보면 정부가 보전해 주는 제도가 있나요?"
    assert norm({"help": ["해외 주식 투자에서 손실을 보전해 주는 제도"]}, question=q2)["help"]   # 낱말이 모두 질문에 있음
    assert "help" not in norm({"help": ["청년 지원", "받을 수 있는 지원"]},                     # 특정하지 않는 말뿐: 넓은 질문
                              question="27살이 받을 수 있는 청년 지원이 뭐가 있나요?")
    q3 = "자격증 시험이나 어학 시험 응시료를 지원받을 수 있나요?"                           # 질문 어미는 잘라 낸다 (이유 문장용)
    assert norm({"help": [q3[:-1]]}, question=q3)["help"] == ["자격증 시험이나 어학 시험 응시료"]
    q4 = "대학생인데 자취방을 구해야 해요. 주거비를 지원받을 수 있을까요?"                  # '있을까요' 앞에서 잘려 온 경우
    assert norm({"help": ["주거비를 지원받을 수"]}, question=q4)["help"] == ["주거비"]


def test_extract_conditions_asks_help_separately(monkeypatch):
    def _chat(system, user):
        return {"asked": ["기초연금"]} if system == condition_extractor.HELP_PROMPT else {}   # 조건은 없어도
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", _chat)
    assert condition_extractor.extract_conditions("기초연금은 어떻게 신청하나요?") == {"help": ["기초연금"]}


def test_service_not_meeting_need_is_dropped(fake_search, fake_conditions, monkeypatch):
    case = fake_search("sr1")
    fake_conditions({"conditions": {"field": "문화", "help": ["관람비"]}})    # 원하는 도움: 관람비
    seen = {}

    def _chat(system, user):
        if system == generator.SYSTEM_PROMPT:        # 자격 체크리스트 추출 호출과 구분
            seen["prompt"] = user
        return {"services": [{"service_id": c.service_id, "applicable": "확인 필요", "meets_need": False}
                             for c in case["results"]]}
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", _chat)
    answer = generator.answer_question(case["question"])
    assert "[사용자가 원하는 도움]" in seen["prompt"]                          # 코드가 만든 도움 목록을 LLM에 준다
    assert answer.no_evidence                                                # 지역·나이만 맞는 제도는 안내하지 않음


def test_meets_need_ignored_when_no_needs(fake_search, fake_conditions, monkeypatch):
    case = fake_search("sr1")
    fake_conditions({"conditions": {"age": 27, "region": "서울"}})           # 넓게 묻는 질문: 원하는 도움 없음
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", lambda s, u: {"services": [
        {"service_id": c.service_id, "applicable": "확인 필요", "meets_need": False} for c in case["results"]]})
    assert not generator.answer_question(case["question"]).no_evidence      # 거르지 않는다



def test_reason_age_pairs_only_with_age_and_no_overlap(monkeypatch):
    ev = evidence.select_evidence(CASES["sr1"]["results"])
    sid = list(ev)[0]
    _fake_llm(monkeypatch, {"services": [{"service_id": sid, "applicable": "확인 필요", "matches": [
        {"said": "서울에 사는 27살", "condition": "서울에 거주하며"},
        {"said": "27살", "condition": "무주택 청년"},                    # 나이 구절 ↔ 나이 아닌 조건 -> 버림
        {"said": "서울에", "condition": "서울에 거주하며"},               # 다른 구절에 포함 -> 버림
    ]}]})
    reason = generator.generate_answer(QUESTION_WITH_RESULTS, {sid: ev[sid]}).recommendations[0].reason
    assert reason.count("‘") == 2 and "‘서울에 사는 27살’" in reason      # 질문 구절 1개 + 근거 구절 1개