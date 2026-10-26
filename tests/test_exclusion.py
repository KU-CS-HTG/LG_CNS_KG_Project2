"""W1: 이미 이용 중인 제도 제외. LLM 호출은 모두 가짜로 바꿔 끼운다."""
import pytest

from src.common.schema import Chunk
from src.generate import condition_extractor as ce
from src.generate import exclusion

Q = "나는 지금 K-Digital Training을 전액지원으로 듣고 있는데, 이것 말고도 취업준비생이 받을 수 있는 정부지원제도가 뭐가 있어?"


def chunk(sid, name, text="취업 지원"):
    return Chunk(chunk_id=f"{sid}#지원내용", service_id=sid, service_name=name, section="지원내용", text=f"{name}\n{text}")


CHUNKS = [chunk("K1", "K-디지털 트레이닝"), chunk("P1", "구직자 직업훈련 및 훈련장려금 지급(한국폴리텍대학)"),
          chunk("Y1", "청년도전지원사업 안내"), chunk("N1", "국민취업지원제도")]


@pytest.fixture
def calls(monkeypatch):
    log = []

    def fake(system, user):
        log.append(system)
        if system is ce.IN_USE_PROMPT:
            return {"in_use": ["K-Digital Training", "지어낸 제도"]}
        if system is exclusion.RESOLVE_PROMPT:
            return {"same": ["K1", "없는ID"], "similar": ["P1"]}
        if system is ce.HELP_PROMPT:
            return {"asked": ["받을 수 있는 정부지원제도"]}
        return {}

    monkeypatch.setattr(ce, "_chat_json", fake)
    return log


# --- 조건 추출 ---------------------------------------------------------------------------------------------
def test_in_use_is_extracted_and_only_names_in_the_question_survive(calls):
    got = ce.extract_conditions(Q)
    assert got["in_use"] == ["K-Digital Training"]            # '지어낸 제도'는 질문 원문에 없어서 버려진다
    assert ce.to_filters(got).get("in_use") is None            # 검색 필터에는 들어가지 않는다


def test_no_third_call_when_question_has_no_in_use_hint(calls):
    got = ce.extract_conditions("서울에 사는 27살 취업준비생인데 월세가 부담돼요")
    assert ce.IN_USE_PROMPT not in calls and "in_use" not in got


def test_in_use_failure_does_not_break_extraction(monkeypatch):
    def fake(system, user):
        if system is ce.IN_USE_PROMPT:
            raise RuntimeError("boom")
        return {"age": 27} if system is ce.SYSTEM_PROMPT else {"asked": []}

    monkeypatch.setattr(ce, "_chat_json", fake)
    got = ce.extract_conditions("나는 27살이고 K-Digital Training을 듣고 있어요")
    assert got["age"] == 27 and "in_use" not in got


def test_in_use_accepts_string_and_caps_length():
    q = "청년도약계좌에 이미 가입했어요"
    assert ce._normalize_conditions({"in_use": "청년도약계좌"}, question=q)["in_use"] == ["청년도약계좌"]
    assert "in_use" not in ce._normalize_conditions({"in_use": ["가" * 40]}, question="가" * 40)
    assert "in_use" not in ce._normalize_conditions({"in_use": [123, None]}, question=q)


# --- 제외 ----------------------------------------------------------------------------------------------------
def test_same_program_removed_and_similar_moved_back_keeping_order(calls):
    out = exclusion.exclude_in_use(Q, {"in_use": ["K-Digital Training"]}, CHUNKS)
    assert [c.service_id for c in out] == ["Y1", "N1", "P1"]    # K1 제외, P1은 맨 뒤, 나머지 순서 유지


def test_unchanged_when_nothing_in_use(calls):
    assert exclusion.exclude_in_use(Q, {}, CHUNKS) is CHUNKS
    assert exclusion.exclude_in_use(Q, {"in_use": []}, CHUNKS) is CHUNKS
    assert ce.IN_USE_PROMPT not in calls and exclusion.RESOLVE_PROMPT not in calls     # LLM을 부르지 않는다


def test_name_match_works_without_llm_and_llm_failure_falls_back(monkeypatch):
    def boom(system, user):
        raise RuntimeError("down")

    monkeypatch.setattr(ce, "_chat_json", boom)
    out = exclusion.exclude_in_use("", {"in_use": ["국민취업지원제도"]}, CHUNKS)
    assert [c.service_id for c in out] == ["K1", "P1", "Y1"]    # 이름이 같은 N1만 빠지고 나머지는 그대로
    out = exclusion.exclude_in_use("", {"in_use": ["K-Digital Training"]}, CHUNKS)
    assert [c.service_id for c in out] == ["K1", "P1", "Y1", "N1"]   # 영어 이름은 LLM 없이는 못 잡으므로 아무것도 빼지 않는다


def test_ids_the_llm_invented_are_ignored_and_same_beats_similar(monkeypatch):
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: {"same": ["X9"], "similar": ["K1", "X9"]})
    two = [chunk("K1", "A 제도 이름이 길다"), chunk("K2", "B 제도 이름이 길다")]
    out = exclusion.exclude_in_use("", {"in_use": ["다른 제도"]}, two)
    assert [c.service_id for c in out] == ["K2", "K1"]          # X9는 후보에 없어 무시, K1은 뒤로


def test_short_names_do_not_match_by_accident(monkeypatch):
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: {})
    out = exclusion.exclude_in_use("", {"in_use": ["청년"]}, CHUNKS)    # 2글자 이름은 코드 비교에 쓰지 않는다
    assert out == CHUNKS


def test_answer_flow_drops_in_use_service_before_evidence_selection(fake_search, fake_conditions, monkeypatch):
    from src.common.schema import Answer
    from src.generate import generator

    case = fake_search("sr1")
    first = case["results"][0]
    fake_conditions({"conditions": {**case["conditions"], "in_use": [first.service_name]}})
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: {})                       # LLM 판별 없이 이름 비교만
    result = type("R", (), {"chunks": list(case["results"]), "filters_used": {}, "relaxed": False})()
    monkeypatch.setattr(generator.hybrid, "search_with_info", lambda *a, **k: result)
    monkeypatch.setattr(generator, "_generate", lambda *a, **k: Answer(text="x"))
    monkeypatch.setattr(generator.evidence_mod, "select_evidence", lambda chunks, n: exclusion._by_service(chunks))   # 색인 없이
    _, selected = generator._answer_with_evidence(case["question"])
    assert first.service_id not in selected and selected                         # 이용 중인 제도만 빠지고 나머지는 남는다
