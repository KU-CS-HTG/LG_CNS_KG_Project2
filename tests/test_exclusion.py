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


ANALYSIS = {"in_use": [{"name": "K-Digital Training", "aliases": ["K-디지털 트레이닝"], "kind": "직업훈련"}],
            "candidates": [{"id": "K1", "kind": "직업훈련", "reason": "디지털 훈련 과정"},
                           {"id": "P1", "kind": "직업훈련", "reason": "직업훈련 과정과 훈련장려금"},
                           {"id": "Y1", "kind": "소득·수당 지원", "reason": "참여수당"},
                           {"id": "N1", "kind": "소득·수당 지원", "reason": "구직촉진수당 등 소득 지원이 핵심"}]}


@pytest.fixture
def calls(monkeypatch):
    log = []

    def fake(system, user):
        log.append(system)
        if system is ce.IN_USE_PROMPT:
            return {"in_use": ["K-Digital Training", "지어낸 제도"]}
        if system is exclusion.RESOLVE_PROMPT:
            return ANALYSIS
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
def test_same_name_and_same_kind_removed_but_other_kinds_kept(calls):
    out = exclusion.exclude_in_use(Q, {"in_use": ["K-Digital Training"]}, CHUNKS)
    # K1: 영문 이름의 한글 표기(aliases)와 이름이 맞음, P1: 같은 종류(직업훈련). 국민취업지원제도(N1)는 종류가 달라 남는다
    assert [c.service_id for c in out] == ["Y1", "N1"]


def test_program_with_training_component_is_kept_when_its_core_kind_differs(monkeypatch):
    analysis = {**ANALYSIS, "candidates": [{"id": "N1", "kind": "취업알선·상담", "reason": "훈련은 일부"}]}
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: analysis)
    out = exclusion.exclude_in_use("", {"in_use": ["K-Digital Training"]}, CHUNKS)
    assert "N1" in [c.service_id for c in out]


def test_unchanged_when_nothing_in_use(calls):
    assert exclusion.exclude_in_use(Q, {}, CHUNKS) is CHUNKS
    assert exclusion.exclude_in_use(Q, {"in_use": []}, CHUNKS) is CHUNKS
    assert ce.IN_USE_PROMPT not in calls and exclusion.RESOLVE_PROMPT not in calls     # LLM을 부르지 않는다


def test_llm_failure_falls_back_to_name_match_only(monkeypatch):
    def boom(system, user):
        raise RuntimeError("down")

    monkeypatch.setattr(ce, "_chat_json", boom)
    out = exclusion.exclude_in_use("", {"in_use": ["국민취업지원제도"]}, CHUNKS)
    assert [c.service_id for c in out] == ["K1", "P1", "Y1"]    # 이름이 같은 N1만 빠진다
    out = exclusion.exclude_in_use("", {"in_use": ["K-Digital Training"]}, CHUNKS)
    assert [c.service_id for c in out] == ["K1", "P1", "Y1", "N1"]   # 영어 이름은 LLM 없이는 못 잡으므로 아무것도 빼지 않는다


def test_invented_ids_and_unknown_kinds_are_ignored(monkeypatch):
    analysis = {"in_use": [{"name": "다른 제도", "aliases": [], "kind": "직업훈련"}],
                "candidates": [{"id": "X9", "kind": "직업훈련"}, {"id": "K2", "kind": "아무 종류"}, {"id": "K1", "kind": "직업훈련"}]}
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: analysis)
    two = [chunk("K1", "A 제도 이름이 길다"), chunk("K2", "B 제도 이름이 길다")]
    out = exclusion.exclude_in_use("", {"in_use": ["다른 제도"]}, two)
    assert [c.service_id for c in out] == ["K2"]                # 목록에 없는 종류는 '기타'라 같다고 보지 않고, X9는 후보에 없다


def test_similar_ones_come_back_when_nothing_else_is_left(monkeypatch):
    analysis = {"in_use": [{"name": "다른 제도", "aliases": [], "kind": "직업훈련"}],
                "candidates": [{"id": "K1", "kind": "직업훈련"}, {"id": "K2", "kind": "직업훈련"}]}
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: analysis)
    two = [chunk("K1", "A 제도 이름이 길다"), chunk("K2", "B 제도 이름이 길다")]
    assert [c.service_id for c in exclusion.exclude_in_use("", {"in_use": ["다른 제도"]}, two)] == ["K1", "K2"]


def test_short_names_do_not_match_by_accident(monkeypatch):
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: {})
    assert exclusion.exclude_in_use("", {"in_use": ["청년"]}, CHUNKS) == CHUNKS    # 2글자 이름은 코드 비교에 쓰지 않는다


def test_trace_explains_each_candidate(calls, capsys):
    from src.generate import trace
    trace.set_enabled(True)
    try:
        exclusion.exclude_in_use(Q, {"in_use": ["K-Digital Training"]}, CHUNKS)
    finally:
        trace.set_enabled(None)
    err = capsys.readouterr().err
    assert "같은 제도: 이름 'K-디지털 트레이닝' 일치" in err and "같은 종류 '직업훈련'" in err
    assert "N1  국민취업지원제도  <- 종류 '소득·수당 지원'" in err and "구직촉진수당" in err


def test_trace_is_silent_when_off(calls, capsys):
    exclusion.exclude_in_use(Q, {"in_use": ["K-Digital Training"]}, CHUNKS)
    assert capsys.readouterr().err == ""


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


# --- 특정 대상 전용 제도 거르기 + 선정 추적 -----------------------------------------------------------------------
def _judged(sid, **kw):
    return {"service_id": sid, "applicable": "확인 필요", "meets_need": True, "matches": [], "needs_check": [], **kw}


def test_group_only_program_is_dropped_not_just_moved_back(capsys):
    from src.generate import generator, trace
    evidence = {"G1": [chunk("G1", "북한이탈주민 일자리성공패키지")], "N1": [chunk("N1", "국민취업지원제도")]}
    out = {"services": [_judged("G1"), _judged("N1")]}
    trace.set_enabled(True)
    try:
        recs = generator._recommendations("취업준비생이 받을 수 있는 정부지원제도 알려줘", evidence, [], None, out, {})
    finally:
        trace.set_enabled(None)
    assert [r.service_id for r in recs] == ["N1"]
    err = capsys.readouterr().err
    assert "G1" in err and "특정 대상 전용 제도인데 질문에 그 대상이 없음" in err
    assert "안내 후보 순서" in err and "1. N1" in err


def test_group_program_is_kept_when_question_mentions_the_group():
    from src.generate import generator
    evidence = {"G1": [chunk("G1", "북한이탈주민 일자리성공패키지")]}
    recs = generator._recommendations("북한이탈주민인데 일자리 지원 있나요", evidence, [], None, {"services": [_judged("G1")]}, {})
    assert [r.service_id for r in recs] == ["G1"]


def test_explain_cli_prints_usage_without_question(capsys):
    from src.generate import explain
    assert explain.main([]) == 2 and "사용" in capsys.readouterr().err
