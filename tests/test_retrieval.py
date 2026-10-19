"""B: retrieve/hybrid.py를 샘플 조각으로 종단 검증. 색인은 임시 폴더 + 가짜 임베딩(conftest의 indexed_sample)."""
import pytest

from src.index import vector_store
from src.common.schema import Chunk
from src.retrieve import filters as filter_rules
from src.retrieve import hybrid

from tests.fixtures import load_sample_queries, service_ids


def test_bm25_exact_name(indexed_sample):
    hits = vector_store.bm25_search("국민내일배움카드", k=3)
    assert hits[0][0].service_id == "SAMPLE012"


def test_vector_search_returns_chunks(indexed_sample):
    hits = vector_store.vector_search("월세가 부담돼요", k=5)
    assert hits
    assert any(c.service_id in {"SAMPLE001", "SAMPLE002"} for c, _ in hits)


def test_dedupe_one_chunk_per_service(sample_chunks):
    sid = service_ids(hybrid.dedupe(sample_chunks))
    assert len(sid) == len(set(sid))


@pytest.mark.parametrize("q", load_sample_queries(), ids=lambda q: q["id"])
def test_search_expectations(indexed_sample, q):
    results = hybrid.search(q["query"], q["filters"], k=5, mode=q.get("mode", "hybrid_filter"))
    ids = service_ids(results)
    if "expect_top1" in q:
        assert ids[0] == q["expect_top1"]
    for sid in q.get("expect_in_top5", []):
        assert sid in ids[:5]
    for sid in q.get("expect_not_in_results", []):
        assert sid not in ids


# --- RRF / 중복 제거 (색인 없이 시험) ---------------------------------------------
def _c(cid, sid=None):
    return Chunk(cid, sid or cid, cid, "지원대상", cid, {}, "")


def test_rrf_combines_ranks():
    a, b, c = _c("a"), _c("b"), _c("c")
    fused = hybrid.rrf([[a, b, c], [c, a]])
    order = [ch.chunk_id for ch, _ in fused]
    assert order[0] == "a"                      # 두 목록 모두 상위 (1위, 2위)
    assert set(order) == {"a", "b", "c"}
    assert [s for _, s in fused] == sorted([s for _, s in fused], reverse=True)


def test_rrf_weights():
    a, b = _c("a"), _c("b")
    assert hybrid.rrf([[a], [b]], weights=(1.0, 2.0))[0][0].chunk_id == "b"


def test_dedupe_keeps_first_per_service():
    kept = hybrid.dedupe([_c("s1#a", "s1"), _c("s2#a", "s2"), _c("s1#b", "s1")])
    assert [c.chunk_id for c in kept] == ["s1#a", "s2#a"]


# --- 검색 모드·필터 완화 (색인 필요) ---------------------------------------------
def test_one_chunk_per_service_and_ranked(indexed_sample):
    result = hybrid.search_with_info("월세가 부담돼요", {"today": "2026-10-19"}, k=5)
    ids = service_ids(result.chunks)
    assert len(ids) == len(result.chunks) <= 5                       # 서비스당 1개
    assert result.scores == sorted(result.scores, reverse=True)      # 관련도 순
    assert not set(ids) & {"SAMPLE003", "SAMPLE010"}                 # 기한 지난 제도 없음


def test_modes_without_filter_keep_everything(indexed_sample):
    """vector/bm25/hybrid 모드는 필터를 쓰지 않는다 (비교 실험용): 부산 제도도 나온다."""
    for mode in ("vector", "bm25", "hybrid"):
        ids = service_ids(hybrid.search("청년월세 지원", {"region": "서울", "today": "2026-10-19"}, k=10, mode=mode))
        assert "SAMPLE002" in ids, mode
    assert "SAMPLE002" not in service_ids(
        hybrid.search("청년월세 지원", {"region": "서울", "today": "2026-10-19"}, k=10, mode="hybrid_filter"))


def test_filter_relaxed_when_no_result(indexed_sample):
    """분야 조건이 너무 엄격해 결과가 없으면 분야를 풀어 다시 찾는다."""
    result = hybrid.search_with_info("월세 지원", {"field": "없는분야", "region": "서울", "today": "2026-10-19"})
    assert result.chunks and result.relaxed
    assert "field" not in result.filters_used and result.filters_used["region"] == "서울"


def test_no_evidence_returns_empty(indexed_sample):
    """모든 조건을 풀어도 없으면(기한 지난 것만 남는 경우) 빈 리스트. 기한 조건은 풀지 않는다."""
    vector_store.remove_services([f"SAMPLE{i:03d}" for i in range(1, 13) if i not in (3, 10)])   # 기한 지난 제도만 남김
    assert hybrid.search("근로장려금", {"today": "2026-10-19"}) == []


def test_invalid_mode(indexed_sample):
    with pytest.raises(ValueError):
        hybrid.search("월세", {}, mode="rerank")


def test_search_on_empty_index(fake_embedder, tmp_vectorstore):
    assert hybrid.search("월세", {"today": "2026-10-19"}) == []


# --- 정형 필터 규칙이 두 곳(파이썬/저장소)에서 같은지 ---------------------------
@pytest.mark.parametrize("flt", [
    {"today": "2026-10-19"},
    {"age": 27, "today": "2026-10-19"},
    {"age": 70, "today": "2026-10-19"},
    {"region": "서울", "today": "2026-10-19"},
    {"region": "부산", "age": 30, "today": "2026-10-19"},
    {"field": "일자리", "today": "2026-10-19"},
    {"age": 27, "region": "서울", "field": "주거", "today": "2026-10-19"},
    {"today": "2026-01-01"},
])
def test_python_filter_matches_store_filter(indexed_sample, flt):
    from_python = {c.service_id for c in filter_rules.apply_filters(indexed_sample, flt)}
    col = vector_store._collection()
    from_store = {m["service_id"] for m in col.get(where=vector_store.build_where(flt), include=["metadatas"])["metadatas"]}
    assert from_python == from_store
