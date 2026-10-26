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
