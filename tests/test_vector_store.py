"""B: index/vector_store.py (Chroma + BM25). 임시 폴더와 가짜 임베딩으로 실제 저장소 동작을 검증한다."""
import pytest

from src.common.schema import Chunk
from src.index import vector_store as vs

TODAY = "2026-10-19"


def _service_ids(hits):
    return {c.service_id for c, _ in hits}


def _ids_matching(filters):
    """where 조건에 맞는 조각의 service_id 집합 (저장소에 직접 질의)."""
    col = vs._collection()
    got = col.get(where=vs.build_where(filters), include=["metadatas"])
    return {m["service_id"] for m in got["metadatas"]}


# --- 색인 -----------------------------------------------------------------------
def test_build_index_counts_and_idempotent(indexed_sample, sample_chunks):
    assert vs.count() == len(sample_chunks)
    vs.build_index(sample_chunks)                       # 같은 조각을 또 넣어도 늘지 않는다 (upsert)
    assert vs.count() == len(sample_chunks)


def test_build_index_empty_is_noop(fake_embedder, tmp_vectorstore):
    vs.build_index([])
    assert vs.count() == 0


def test_roundtrip_chunk_fields(indexed_sample, sample_chunks):
    by_id = {c.chunk_id: c for c in sample_chunks}
    hits = vs.bm25_search("청년월세 서울", k=5)
    assert hits
    for chunk, _ in hits:
        orig = by_id[chunk.chunk_id]
        assert (chunk.service_id, chunk.service_name, chunk.section, chunk.text, chunk.detail_url) == (
            orig.service_id, orig.service_name, orig.section, orig.text, orig.detail_url)
        assert chunk.metadata.get("agency") == orig.metadata.get("agency")
        assert chunk.metadata.get("target_tags", []) == orig.metadata.get("target_tags", [])


def test_missing_metadata_is_omitted_not_zero(indexed_sample):
    """값이 없던 키는 되돌릴 때도 없어야 한다 (대체값 -1, 999, 99999999가 새어 나오면 안 됨)."""
    chunk = vs.get_service_chunks("SAMPLE011")[0]            # 지역·연령·기한이 모두 비어 있는 제도
    for key in ("region", "deadline", "age_min", "age_max"):
        assert key not in chunk.metadata


def test_reindex_removes_stale_sections(indexed_sample, sample_chunks):
    kept = [c for c in sample_chunks if c.service_id == "SAMPLE001" and c.section in ("지원대상", "지원내용")]
    vs.build_index(kept)                                     # 서비스에서 두 항목이 사라진 경우
    assert {c.section for c in vs.get_service_chunks("SAMPLE001")} == {"지원대상", "지원내용"}
    assert len(vs.get_service_chunks("SAMPLE002")) == 4      # 다른 서비스는 그대로


def test_remove_services(indexed_sample):
    vs.remove_services(["SAMPLE001", "SAMPLE002"])
    assert vs.get_service_chunks("SAMPLE001") == []
    assert vs.count() == 40
    assert "SAMPLE001" not in _service_ids(vs.bm25_search("청년월세", k=10))     # BM25 색인도 갱신됨


def test_get_service_chunks_in_section_order(indexed_sample):
    assert [c.section for c in vs.get_service_chunks("SAMPLE004")] == ["지원대상", "선정기준", "지원내용", "신청방법"]


def test_clear_index(indexed_sample, sample_chunks):
    vs.clear_index()
    assert vs.count() == 0
    vs.build_index(sample_chunks)
    assert vs.count() == len(sample_chunks)


def test_collection_per_embedding_model(indexed_sample, monkeypatch):
    from src.common import config

    monkeypatch.setattr(config, "EMBEDDING_MODEL", "intfloat/multilingual-e5-large")
    assert vs.count() == 0                                   # 모델이 바뀌면 다른 컬렉션 (차원이 달라도 안전)


# --- 검색 -----------------------------------------------------------------------
def test_vector_search_scores_and_order(indexed_sample):
    hits = vs.vector_search("월세가 부담돼요", k=5)
    assert 0 < len(hits) <= 5
    scores = [s for _, s in hits]
    assert scores == sorted(scores, reverse=True)
    assert _service_ids(hits) & {"SAMPLE001", "SAMPLE002"}


def test_bm25_exact_name_top1(indexed_sample):
    hits = vs.bm25_search("국민내일배움카드", k=3)
    assert hits[0][0].service_id == "SAMPLE012"
    assert hits[0][1] > 0


def test_bm25_handles_particles_and_short_words(indexed_sample):
    """조사가 붙어도('월세를'), 두 글자 단어('월세')도 찾는다 (글자 2-gram)."""
    assert _service_ids(vs.bm25_search("월세를", k=5)) & {"SAMPLE001", "SAMPLE002"}
    assert _service_ids(vs.bm25_search("월세", k=5)) & {"SAMPLE001", "SAMPLE002"}


def test_bm25_no_match_and_empty_query(indexed_sample):
    assert vs.bm25_search("zzzzzz", k=5) == []
    assert vs.bm25_search("", k=5) == []


def test_search_on_empty_store(fake_embedder, tmp_vectorstore):
    assert vs.vector_search("월세", k=5) == []
    assert vs.bm25_search("월세", k=5) == []


def test_bm25_cache_refreshes_after_index_change(indexed_sample):
    assert vs.bm25_search("신규제도키워드", k=3) == []
    vs.build_index([Chunk("NEW#지원대상", "NEW", "신규제도키워드 지원", "지원대상", "신규제도키워드 지원\n누구나", {}, "u")])
    assert vs.bm25_search("신규제도키워드", k=3)[0][0].service_id == "NEW"


# --- 소프트 필터 (build_where) ---------------------------------------------------
def test_where_age_and_region(indexed_sample):
    ids = _ids_matching({"age": 27, "region": "서울", "today": TODAY})
    assert "SAMPLE001" in ids and "SAMPLE005" in ids       # 서울, 19~34세
    assert "SAMPLE002" not in ids                            # 부산
    assert "SAMPLE009" not in ids                            # 65세 이상
    assert "SAMPLE008" in ids and "SAMPLE004" in ids         # 전국 / 연령 제한 없음 또는 범위 안


def test_where_missing_values_pass(indexed_sample):
    assert "SAMPLE011" in _ids_matching({"age": 27, "region": "서울", "today": TODAY})   # 지역·연령·기한이 모두 비어 있음


def test_where_excludes_expired_always(indexed_sample):
    assert not {"SAMPLE003", "SAMPLE010"} & _ids_matching({"today": TODAY})
    assert not {"SAMPLE003", "SAMPLE010"} & _ids_matching({})          # today 생략: 오늘 기준


def test_where_ignores_field_and_situation(indexed_sample):
    """관심 분야·상황은 필터가 아니다: 분야가 달라도 제도가 사라지지 않는다 (분야 이름이 데이터와 달라도 안전)."""
    base = _ids_matching({"today": TODAY})
    assert _ids_matching({"field": "주거", "today": TODAY}) == base
    assert _ids_matching({"field": "존재하지않는분야", "situation": ["구직"], "today": TODAY}) == base
    assert "SAMPLE004" in _ids_matching({"field": "주거", "today": TODAY})        # 일자리 분야 제도도 남는다


def test_where_shape():
    assert vs.build_where({"today": TODAY}) == {"deadline_int": {"$gte": 20261019}}
    assert vs.build_where({"field": "주거", "situation": ["구직"], "today": TODAY}) == {"deadline_int": {"$gte": 20261019}}
    w = vs.build_where({"age": 27, "today": TODAY})
    assert w == {"$and": [{"age_min": {"$lte": 27}}, {"age_max": {"$gte": 27}}, {"deadline_int": {"$gte": 20261019}}]}


def test_searches_honor_where(indexed_sample):
    where = vs.build_where({"region": "서울", "age": 27, "today": TODAY})
    for fn in (vs.vector_search, vs.bm25_search):
        ids = _service_ids(fn("청년월세 지원", 50, where))
        assert "SAMPLE002" not in ids and "SAMPLE003" not in ids and "SAMPLE010" not in ids
    assert "SAMPLE002" in _service_ids(vs.bm25_search("청년월세 지원", 50, None))   # where=None이면 필터 없음
