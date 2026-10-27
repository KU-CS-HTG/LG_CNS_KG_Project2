"""X1 판정 기준 문서 색인·검색 (src/index/guide_store.py). 실제 모델 대신 낱말 기반 가짜 임베딩을 쓴다."""
import json

import numpy as np
import pytest

from src.common.schema import GuideChunk
from src.index import embedder, guide_store

WORDS = ["부양의무자", "생계급여", "주거급여", "청년", "분리지급", "구직촉진수당", "자동차", "재산"]


def fake_embed(texts, kind="passage"):
    return [[float(t.count(w)) + 0.01 for w in WORDS] for t in texts]


def _chunk(cid, text, service_ids=("S-생계",), scope="전국", page=1):
    return GuideChunk(chunk_id=cid, doc_id=cid.split("#")[0], doc_title=f"{cid} 문서", publisher="보건복지부",
                      year=2026, page=page, heading=f"{cid} 문서 > 절", text=text, scope=scope,
                      service_ids=list(service_ids))


CHUNKS = [
    _chunk("life#p1-1", "부양의무자 기준은 생계급여에서 폐지되었다. 부양의무자의 연소득이 1억 3천만원을 넘으면 예외."),
    _chunk("life#p2-1", "자동차는 재산으로 환산한다. 생업용 자동차는 재산에서 뺀다.", page=2),
    _chunk("house#p1-1", "청년 주거급여 분리지급: 부모와 따로 사는 19세~30세 미만 청년.", service_ids=("S-주거",)),
    _chunk("common#p1-1", "기준 중위소득과 생계급여·주거급여 선정기준.", service_ids=()),
    _chunk("seoul#p1-1", "서울형 기초보장 생계급여 부양의무자 기준.", scope="서울"),
    _chunk("job#p1-1", "구직촉진수당은 월 50만원씩 6개월.", service_ids=("S-취업",)),
]


@pytest.fixture
def index(tmp_path, monkeypatch):
    monkeypatch.setattr(embedder, "embed_texts", fake_embed)
    monkeypatch.setattr(embedder, "embed_query", lambda q: fake_embed([q], "query")[0])
    monkeypatch.setattr(guide_store, "_state", None)
    guide_store.build(CHUNKS, index_dir=tmp_path)
    return tmp_path


def _ids(hits):
    return [h.chunk_id for h in hits]


def test_build_writes_index_files(index):
    manifest = json.loads((index / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["count"] == len(CHUNKS) and manifest["model"]
    assert np.load(index / "vectors.npy").shape == (len(CHUNKS), len(WORDS))


def test_returns_guide_chunks_ranked_by_relevance(index):
    hits = guide_store.search_guides("부양의무자가 있으면 생계급여를 못 받나요?", k=3, index_dir=index)
    assert all(isinstance(h, GuideChunk) for h in hits) and len(hits) <= 3
    assert hits[0].chunk_id == "life#p1-1"


def test_service_ids_keep_that_service_and_common_documents(index):
    hits = guide_store.search_guides("청년 주거급여 분리지급", service_ids=["S-주거"], k=10, index_dir=index)
    assert set(_ids(hits)) <= {"house#p1-1", "common#p1-1"}
    assert hits[0].chunk_id == "house#p1-1"


def test_national_always_passes_and_regional_only_when_region_matches(index):
    q = "생계급여 부양의무자"
    assert "seoul#p1-1" not in _ids(guide_store.search_guides(q, k=10, index_dir=index))          # 지역 모름
    assert "seoul#p1-1" not in _ids(guide_store.search_guides(q, region="부산", k=10, index_dir=index))
    assert "seoul#p1-1" in _ids(guide_store.search_guides(q, region="서울특별시", k=10, index_dir=index))


def test_no_match_or_empty_query_returns_empty(index):
    hits = guide_store.search_guides("생계급여", service_ids=["없는제도"], k=3, index_dir=index)
    assert _ids(hits) == ["common#p1-1"]                                     # 공통 기준 문서만 남는다
    assert guide_store.search_guides("   ", k=3, index_dir=index) == []
    assert guide_store.search_guides("생계급여", k=0, index_dir=index) == []


def test_missing_index_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(guide_store, "_state", None)
    assert guide_store.search_guides("생계급여", index_dir=tmp_path / "none") == []


def test_model_mismatch_is_refused(index, monkeypatch):
    monkeypatch.setattr(guide_store, "_state", None)
    monkeypatch.setattr(guide_store.config, "EMBEDDING_MODEL", "other/model")
    with pytest.raises(RuntimeError, match="모델"):
        guide_store.search_guides("생계급여", index_dir=index)


def test_same_query_same_result(index):
    q = "자동차가 있으면 재산으로 계산되나요?"
    first = guide_store.search_guides(q, service_ids=["S-생계"], k=3, index_dir=index)
    assert first[0].chunk_id == "life#p2-1"
    assert _ids(guide_store.search_guides(q, service_ids=["S-생계"], k=3, index_dir=index)) == _ids(first)


def test_rebuild_reuses_vectors_when_only_metadata_changes(index, monkeypatch):
    """출처 URL·연결 제도만 고치면 다시 임베딩하지 않는다. 본문이 바뀐 조각만 새로 임베딩한다."""
    import dataclasses
    asked = []
    monkeypatch.setattr(embedder, "embed_texts", lambda texts, kind="passage": asked.extend(texts) or fake_embed(texts))
    changed = [dataclasses.replace(c, source_url="https://example.go.kr", service_ids=["S-새"]) for c in CHUNKS]
    guide_store.build(changed, index_dir=index)
    assert asked == []
    changed[0] = dataclasses.replace(changed[0], text="부양의무자 기준이 바뀌었다.")
    guide_store.build(changed, index_dir=index)
    assert asked == [guide_store.embed_text(changed[0])]
    hits = guide_store.search_guides("부양의무자", service_ids=["S-새"], k=1, index_dir=index)
    assert hits[0].source_url == "https://example.go.kr"


def test_guide_evidence_sees_the_module():
    from src.generate import guide_evidence
    assert guide_evidence.available()
