"""B: retrieve/filters.py. 소프트 필터 규칙(값이 비면 통과, 기한 지난 제도 제외, 결과 없으면 완화)."""
from src.retrieve.filters import apply_filters, is_expired, relax

from tests.fixtures import service_ids

TODAY = "2026-10-19"


def _ids(chunks, filters):
    return set(service_ids(apply_filters(chunks, filters)))


def test_region_and_age(sample_chunks):
    ids = _ids(sample_chunks, {"region": "서울", "age": 27, "today": TODAY})
    assert "SAMPLE001" in ids          # 서울 + 19~34세
    assert "SAMPLE002" not in ids      # 부산
    assert "SAMPLE009" not in ids      # 65세 이상
    assert "SAMPLE008" in ids          # 전국, 연령 제한 없음


def test_missing_values_pass(sample_chunks):
    assert "SAMPLE011" in _ids(sample_chunks, {"region": "서울", "age": 27, "today": TODAY})


def test_expired_excluded(sample_chunks):
    ids = _ids(sample_chunks, {"today": TODAY})
    assert "SAMPLE003" not in ids and "SAMPLE010" not in ids


def test_is_expired(sample_chunks):
    by_id = {c.service_id: c for c in sample_chunks}
    assert is_expired(by_id["SAMPLE003"], TODAY)
    assert not is_expired(by_id["SAMPLE001"], TODAY)
    assert not is_expired(by_id["SAMPLE004"], TODAY)   # 기한 없음(상시)


def test_empty_filters_keep_non_expired(sample_chunks):
    assert len(_ids(sample_chunks, {"today": TODAY})) >= 8


def test_relax_eventually_empty():
    f = {"region": "서울", "age": 27, "field": "주거", "today": TODAY}
    for _ in range(10):
        nxt = relax(f)
        assert len(nxt) <= len(f)
        if not {k for k in nxt if k != "today"}:
            return
        f = nxt
    raise AssertionError("relax가 필터를 모두 풀지 못함")
