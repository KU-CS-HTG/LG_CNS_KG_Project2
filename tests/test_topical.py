"""핵심 주제어가 본문에 있는 제도를 판정 후보 앞으로 (src/generate/topical.py)."""
from src.common.schema import Chunk
from src.generate import topical


def chunk(sid, name, text):
    return Chunk(chunk_id=f"{sid}#지원내용", service_id=sid, service_name=name, section="지원내용", text=f"{name}\n{text}")


def test_core_terms_drop_generic_words_and_particles():
    assert topical.core_terms(["대중교통비를 아낄 수 있는 정부나 지자체 지원제도"]) == ["대중교통비"]
    assert topical.core_terms(["월세가 부담", "면접 비용 지원"]) == ["월세", "면접"]
    assert topical.core_terms(["받을 수 있는 지원"]) == [] and topical.core_terms(None) == []


def test_match_score_is_fuzzy_on_two_letter_pairs():
    assert topical.match_score("월 15회 이상 대중교통 이용 시 환급", ["대중교통비"]) >= topical.MATCH_RATIO
    assert topical.match_score("농업기계 구입 지원", ["대중교통비"]) == 0.0


def test_prioritize_moves_topical_services_first_and_keeps_the_rest_in_order():
    chunks = [chunk("A", "농업기계구입지원", "농기계 구입"), chunk("B", "화성시 무상교통", "시내버스 무상"),
              chunk("K", "K-패스", "대중교통 이용 시 환급"), chunk("C", "시티투어", "관람료 감면")]
    out = topical.prioritize(chunks, ["대중교통비를 아낄 수 있는 지원제도"])
    assert [c.service_id for c in out] == ["K", "A", "B", "C"]


def test_prioritize_changes_nothing_without_terms_or_matches():
    chunks = [chunk("A", "가", "내용"), chunk("B", "나", "내용")]
    assert topical.prioritize(chunks, None) is chunks
    assert topical.prioritize(chunks, ["월세 지원"]) is chunks              # 맞는 제도가 없으면 그대로
    assert topical.prioritize([chunk("A", "월세", "월세 지원")], ["월세 지원"]) == [chunk("A", "월세", "월세 지원")]


def test_find_districts_accepts_city_names_without_the_suffix():
    from src.generate import generator
    assert generator._find_districts("나는 지금 수원에 살고 있어", "경기") == {"수원시"}
    assert generator._find_districts("수원 사는 청년이에요", None) == {"수원시"}
    assert generator._find_districts("광주에 살아요", "광주") == set()           # 시·도 이름과 같은 말은 시로 보지 않는다
    assert generator._find_districts("안성맞춤 선물 지원", None) == set()        # 일부가 같은 낱말은 아니다
    assert generator._find_districts("수원에 살아요", "서울") == set()            # 말한 시·도에 없는 도시


def test_phrase_queries_use_core_terms_only():
    assert topical.phrase_queries(["대중교통비를 아낄 수 있는 정부나 지자체 지원제도"], "질문 전체") == ["대중교통비"]
    assert topical.phrase_queries(["받을 수 있는 지원"], "q") == [] and topical.phrase_queries(None) == []
    assert topical.phrase_queries(["월세 지원"], "월세") == []                  # 질문 전체와 같으면 다시 찾지 않는다


def test_answer_flow_adds_core_term_search_results(monkeypatch, fake_conditions):
    from src.common.schema import Answer
    from src.generate import condition_extractor as ce
    from src.generate import generator

    asked = []
    main_hit, core_hit = chunk("M1", "경기 교통비", "교통비"), chunk("K1", "K-패스", "대중교통 이용 시 환급")

    def fake_search(query, filters, k=5, mode="hybrid_filter"):
        asked.append(query)
        hit = core_hit if query == "대중교통비" else main_hit
        return type("R", (), {"chunks": [hit], "filters_used": {}, "relaxed": False})()

    q = "대중교통비를 아낄 수 있는 지원제도가 있어? 나는 지금 수원에 살고 있어."
    fake_conditions({"conditions": {"region": "경기", "help": ["대중교통비를 아낄 수 있는 지원제도"]}})
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: {})
    monkeypatch.setattr(generator.hybrid, "search_with_info", fake_search)
    monkeypatch.setattr(generator, "_generate", lambda *a, **k: Answer(text="x"))
    monkeypatch.setattr(generator, "_application_period", lambda sid: "상시신청")
    monkeypatch.setattr(generator.evidence_mod, "select_evidence", lambda chunks, n: {c.service_id: [c] for c in chunks})
    _, selected = generator._answer_with_evidence(q)
    assert "대중교통비" in asked and {"M1", "K1"} <= set(selected)
