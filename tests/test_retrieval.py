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
    """지역 조건에 맞는 제도가 하나도 없으면 지역을 풀어 다시 찾는다. 분야·상황은 그대로 남는다."""
    vector_store.remove_services([f"SAMPLE{i:03d}" for i in range(1, 13) if i != 2])   # 부산 제도(SAMPLE002)만 남김
    flt = {"region": "서울", "field": "주거", "situation": ["구직"], "today": "2026-10-19"}
    result = hybrid.search_with_info("월세 지원", flt)
    assert [c.service_id for c in result.chunks] == ["SAMPLE002"] and result.relaxed
    assert "region" not in result.filters_used
    assert result.filters_used["field"] == "주거" and result.filters_used["situation"] == ["구직"]   # 분야·상황은 풀리지 않음


def test_field_does_not_remove_other_fields(indexed_sample):
    """관심 분야가 '주거'여도 일자리 분야 제도가 사라지지 않는다 (스크린샷에서 발견된 문제의 회귀 방지)."""
    flt = {"age": 27, "region": "서울", "field": "주거", "today": "2026-10-19"}
    ids = service_ids(hybrid.search("취업 준비 중인데 월세도 부담돼요", flt, k=10))
    assert "SAMPLE001" in ids                                     # 주거 제도
    assert {"SAMPLE004", "SAMPLE005"} & set(ids)                  # 구직 제도 (분야 필터가 있었다면 없었을 것)


def test_field_boost_moves_matching_field_up(indexed_sample):
    q = "지원 안내"
    plain = service_ids(hybrid.search(q, {"today": "2026-10-19"}, k=12))
    boosted = service_ids(hybrid.search(q, {"field": "일자리", "today": "2026-10-19"}, k=12))
    jobs = {"SAMPLE004", "SAMPLE005", "SAMPLE006"}                        # 샘플에서 분야가 일자리인 제도 (기한이 지난 010 제외)
    pos = lambda ids: [i for i, s in enumerate(ids) if s in jobs]
    assert set(plain) == set(boosted)                              # 가산점은 거르지 않는다: 같은 제도가 나온다
    assert sum(pos(boosted)) <= sum(pos(plain))                    # 일자리 제도는 앞으로 오거나 그대로


def test_field_boost_unit():
    def ch(cid, field):
        return Chunk(cid, cid, cid, "지원대상", cid, {"field": field} if field else {}, "")
    ranked = [(ch("a", "일자리"), 0.0165), (ch("b", "주거·자립"), 0.0160), (ch("c", None), 0.0150)]
    assert [c.chunk_id for c, _ in hybrid.boost_by_field(ranked, "주거", boost=0.002)] == ["b", "a", "c"]   # 포함 관계도 맞음
    assert [c.chunk_id for c, _ in hybrid.boost_by_field(ranked, "존재하지않음", boost=0.002)] == ["a", "b", "c"]   # 안 맞아도 그대로
    assert hybrid.boost_by_field(ranked, "주거", boost=0) == ranked and hybrid.boost_by_field(ranked, None) == ranked


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


def test_penalize_local_reorders_without_dropping():
    from src.common.schema import Chunk
    from src.retrieve import hybrid

    def c(sid, agency):
        return Chunk(f"{sid}#a", sid, sid, "지원대상", "t", {"agency": agency}, "")

    ranked = [(c("L1", "서울특별시 강서구"), 0.0160), (c("N1", "국토교통부"), 0.0150), (c("L2", "경기도"), 0.0140), (c("N2", "고용노동부"), 0.0130)]
    assert [x.service_id for x, _ in hybrid.penalize_local(ranked, 0.0)] == ["L1", "N1", "L2", "N2"]         # 끔
    assert [x.service_id for x, _ in hybrid.penalize_local(ranked, 0.0015)] == ["N1", "L1", "N2", "L2"]     # 0.0145, 0.0150, 0.0125, 0.0130
    assert [x.service_id for x, _ in hybrid.penalize_local(ranked, 1.0)] == ["N1", "N2", "L1", "L2"]         # 지역 제도는 뒤로, 사라지지 않음
