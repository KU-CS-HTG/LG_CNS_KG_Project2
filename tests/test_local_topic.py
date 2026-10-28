"""지역 없는 질문에서 주제가 지자체 제도에만 있으면 지자체 제도를 후보에 넣는다 (src/generate/local_topic.py).

검색은 가짜로 바꿔 끼운다 (모델·색인 없음)."""
import pytest

from src.common.schema import Chunk
from src.generate import local_topic
from src.retrieve import hybrid


def chunk(sid, name, text, region="전국", **md):
    return Chunk(chunk_id=f"{sid}#지원내용", service_id=sid, service_name=name, section="지원내용",
                 text=f"{name}\n{text}", metadata={"region": region, **md})


NATIONAL = [chunk("N1", "입원·격리치료명령 결핵환자 부양가족생활보호비 지원", "생활보호비 지급"),
            chunk("N2", "생계급여", "생계급여 지급")]
PET_SEOUL = chunk("L1", "취약계층 반려동물 의료비 지원", "반려동물 진료비 지원", region="서울")
PET_SEOUL_2 = chunk("L1b", "취약계층 반려동물 의료비 지원", "반려동물 진료비 지원", region="서울")
PET_GYEONGGI = chunk("L2", "돌봄 취약가구 반려동물 의료서비스 등 지원", "반려동물 의료서비스", region="경기")
PET_NATIONAL = chunk("N3", "반려동물 등록 안내", "반려동물 등록")
OTHER_LOCAL = chunk("L3", "청년 월세 지원", "월세 지원", region="대전")


@pytest.fixture
def searched(monkeypatch):
    calls = []

    class Result:
        def __init__(self, chunks):
            self.chunks = chunks

    def fake(query, filters, k=5, mode="hybrid_filter"):
        calls.append((query, mode))
        return Result([PET_NATIONAL, OTHER_LOCAL, PET_SEOUL, PET_SEOUL_2, PET_GYEONGGI])
    monkeypatch.setattr(hybrid, "search_with_info", fake)
    return calls


def test_topic_only_in_local_programs_adds_them_without_penalty(searched):
    added, terms = local_topic.local_candidates(NATIONAL, ["반려동물 병원비"], {})
    assert terms == ["반려동물", "병원비"]
    assert searched == [("반려동물 병원비", "hybrid")]                     # 지역 감점 없는 검색
    # 주제가 맞는 지자체 제도만, 같은 이름은 하나만 (전국 제도·주제가 다른 지자체 제도는 넣지 않는다)
    assert [c.service_id for c in added] == ["L1", "L2"]


def test_topic_already_covered_keeps_national_first(searched):
    """'대학 등록금'처럼 앞쪽 후보에 주제가 있으면 다시 찾지 않는다 (지금처럼 전국 제도 먼저)."""
    covered = NATIONAL + [chunk("N4", "국가장학금", "대학 등록금 지원")]
    assert local_topic.local_candidates(covered, ["대학 등록금 지원"], {}) == ([], [])
    assert searched == []


def test_region_given_or_no_topic_does_nothing(searched):
    assert local_topic.local_candidates(NATIONAL, ["반려동물 병원비"], {"region": "서울"}) == ([], [])
    assert local_topic.local_candidates(NATIONAL, ["받을 수 있는 지원"], {}) == ([], [])
    assert searched == []


def test_age_and_deadline_rules_still_apply(searched):
    """감점 없는 검색은 필터 없는 모드라 나이·기한은 B와 같은 규칙(apply_filters)으로 거른다."""
    global PET_SEOUL
    young_only = chunk("L1", "취약계층 반려동물 의료비 지원", "반려동물 진료비 지원", region="서울", age_max=30)
    PET_SEOUL, saved = young_only, PET_SEOUL
    try:
        added, _ = local_topic.local_candidates(NATIONAL, ["반려동물 병원비"], {"age": 40})
    finally:
        PET_SEOUL = saved
    assert [c.service_id for c in added] == ["L1b", "L2"]                 # 40세는 30세 이하 제도에서 빠진다


def test_notice_asks_for_region():
    text = local_topic.notice(["반려동물"])
    assert "'반려동물'" in text and "지역" in text
    assert "'반려동물 병원비 지원'" in local_topic.notice(["병원비"], ["생계급여", "반려동물 병원비 지원"])
