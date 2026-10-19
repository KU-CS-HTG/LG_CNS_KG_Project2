"""B: index/vector_store.py + retrieve/hybrid.py를 샘플 조각으로 종단 검증.

주의: build_index는 data/vectorstore/를 쓴다. 테스트용 저장 경로 분리는 B가 결정한다
(예: 환경변수나 인자로 임시 경로 주입). 정해지면 이 fixture를 고친다.
"""
import pytest

from src.index import vector_store
from src.retrieve import hybrid

from tests.fixtures import load_sample_queries, service_ids


@pytest.fixture(scope="module")
def indexed(sample_chunks):
    vector_store.build_index(sample_chunks)
    return sample_chunks


def test_bm25_exact_name(indexed):
    hits = vector_store.bm25_search("국민내일배움카드", k=3)
    assert hits[0][0].service_id == "SAMPLE012"


def test_vector_search_returns_chunks(indexed):
    hits = vector_store.vector_search("월세가 부담돼요", k=5)
    assert hits
    assert any(c.service_id in {"SAMPLE001", "SAMPLE002"} for c, _ in hits)


def test_dedupe_one_chunk_per_service(sample_chunks):
    sid = service_ids(hybrid.dedupe(sample_chunks))
    assert len(sid) == len(set(sid))


@pytest.mark.parametrize("q", load_sample_queries(), ids=lambda q: q["id"])
def test_search_expectations(indexed, q):
    results = hybrid.search(q["query"], q["filters"], k=5, mode=q.get("mode", "hybrid_filter"))
    ids = service_ids(results)
    if "expect_top1" in q:
        assert ids[0] == q["expect_top1"]
    for sid in q.get("expect_in_top5", []):
        assert sid in ids[:5]
    for sid in q.get("expect_not_in_results", []):
        assert sid not in ids
