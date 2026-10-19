"""샘플 데이터가 데이터 계약(src/common/schema.py)을 지키는지 확인. 항상 실행된다."""
from src.common.schema import SECTIONS

from tests.fixtures import load_sample_queries, load_search_cases, service_ids

METADATA_KEYS = {"region", "agency", "field", "deadline", "age_min", "age_max", "target_tags"}


def test_chunk_contract(sample_chunks):
    assert sample_chunks
    ids = [c.chunk_id for c in sample_chunks]
    assert len(ids) == len(set(ids))
    for c in sample_chunks:
        assert c.section in SECTIONS
        assert c.chunk_id == f"{c.service_id}#{c.section}"
        assert c.text.startswith(c.service_name)          # 서비스명 접두
        assert set(c.metadata) <= METADATA_KEYS
        assert c.detail_url


def test_sample_covers_tricky_cases(sample_chunks):
    md = {c.service_id: c.metadata for c in sample_chunks}
    assert any(m.get("deadline", "9999") < "2026-10-19" for m in md.values())   # 기한 지난 제도
    assert any("region" not in m and "age_min" not in m for m in md.values())   # 값이 빈 제도
    names = {c.service_name for c in sample_chunks}
    assert {"청년월세 지원(서울)", "청년월세 지원(부산)"} <= names                # 지역만 다른 중복


def test_queries_reference_existing_services(sample_chunks):
    known = {c.service_id for c in sample_chunks}
    for q in load_sample_queries():
        refs = set(q.get("expect_in_top5", [])) | set(q.get("expect_not_in_results", []))
        if "expect_top1" in q:
            refs.add(q["expect_top1"])
        assert refs <= known, q["id"]


def test_search_cases_follow_b_to_c_contract():
    """B -> C 샘플이 인계 규칙(서비스당 1개, 최대 5개)을 지키는지 확인."""
    cases = load_search_cases()
    assert {"sr1", "sr2"} <= {c["id"] for c in cases}
    for c in cases:
        ids = service_ids(c["results"])
        assert len(c["results"]) <= 5
        assert len(ids) == len(c["results"])               # 서비스당 조각 1개 (중복 제거됨)
        assert {"today"} <= set(c["filters"])
    assert any(not c["results"] for c in cases)            # 근거 없음 사례 포함
