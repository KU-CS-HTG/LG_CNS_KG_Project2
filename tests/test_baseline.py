"""retrieve/baseline.py (SQL + 키워드) 점검. 임시 SQLite만 쓰므로 모델·색인이 필요 없다."""
import pytest

from src.common import config
from src.ingest import storage, store
from src.retrieve import baseline


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "services.db")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    p = storage.StoragePaths(tmp_path / "state" / "services.db", tmp_path / "state" / "raw", "test").validated()
    rows = [
        ("S1", "청년 월세 지원", "무주택 청년의 월세를 지원", "월세 대출"),
        ("S2", "국민취업지원제도", "구직 중인 청년 취업준비", "구직촉진수당 월세 아님"),
        ("S3", "청년 학자금 지원", "대학생 등록금", "장학금"),
        ("S4", "월세 보증", "청년 월세 보증금 보증", "월세 월세 월세"),
    ]
    store.upsert_services([store.clean_service({
        "서비스ID": sid, "서비스명": name, "지원대상": target, "지원내용": content, "소관기관명": "기관",
        "상세조회URL": "https://www.gov.kr/synthetic/" + sid}) for sid, name, target, content in rows], storage=p)
    return p


def test_extract_keywords_strips_particles_and_stopwords():
    kw = baseline.extract_keywords("월세가 부담되고 취업을 준비하고 있는데 받을 수 있는 지원이 있을까요?")
    assert "월세" in kw and "취업" in kw and "지원" not in kw and "있을까요" not in kw
    assert baseline.extract_keywords("서울에서 27살") == ["서울", "27살"]
    assert baseline.extract_keywords("") == [] and baseline.extract_keywords("은 는 이") == []


def test_search_ranks_by_distinct_keyword_hits(paths):
    got = baseline.search("월세 취업", {}, 3, storage=paths)
    ids = [c.service_id for c in got]
    assert ids == ["S2", "S1", "S4"]            # S2: 이름 '취업'+본문 월세·취업, S1·S4: 이름+본문 '월세'(동점은 ID 순)
    assert len(ids) == len(set(ids))                                # 서비스당 조각 1개


def test_search_name_match_weighted_and_top_k(paths):
    got = baseline.search("월세", {}, 1, storage=paths)
    assert [c.service_id for c in got] == ["S1"]              # 이름·본문에 월세, 동점 S4보다 ID가 빠름
    assert len(baseline.search("월세", {}, 2, storage=paths)) == 2


def test_search_ignores_filters_and_handles_empty(paths):
    a = baseline.search("월세", {"region": "부산", "age": 70}, 5, storage=paths)
    b = baseline.search("월세", {}, 5, storage=paths)
    assert [c.service_id for c in a] == [c.service_id for c in b]
    assert baseline.search("   ", {}, 5, storage=paths) == []
    assert baseline.search("월세", {}, 0, storage=paths) == []
    assert baseline.search("존재하지않는단어xyz", {}, 5, storage=paths) == []


def test_chunk_is_valid_with_url(paths):
    c = baseline.search("학자금", {}, 5, storage=paths)[0]
    assert c.service_id == "S3" and c.detail_url.endswith("S3") and c.text.startswith("청년 학자금 지원")


def test_active_count(paths, tmp_path):
    assert baseline.active_count(storage=paths) == 4
    empty = storage.StoragePaths(tmp_path / "none" / "x.db", tmp_path / "none" / "raw", "test").validated()
    assert baseline.active_count(storage=empty) == 0 and not empty.db_path.exists()    # 빈 DB를 만들지 않음
