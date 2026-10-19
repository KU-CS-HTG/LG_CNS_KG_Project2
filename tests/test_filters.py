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


def test_relax_order_and_keeps_today():
    f = {"age": 27, "region": "서울", "field": "주거", "situation": ["구직"], "today": TODAY}
    steps = [f]
    while True:
        nxt = relax(steps[-1])
        if nxt == steps[-1]:
            break
        steps.append(nxt)
    removed = [set(a) - set(b) for a, b in zip(steps, steps[1:])]
    assert removed == [{"field"}, {"region"}, {"age"}, {"situation"}]     # 분야 -> 지역 -> 연령 -> situation
    assert all(s["today"] == TODAY for s in steps)                         # 신청기한은 풀지 않는다
    assert steps[-1] == {"today": TODAY}


def test_relax_does_not_mutate_input():
    f = {"region": "서울", "today": TODAY}
    relax(f)
    assert f == {"region": "서울", "today": TODAY}


def test_is_expired_defaults_to_today(sample_chunks):
    by_id = {c.service_id: c for c in sample_chunks}
    assert is_expired(by_id["SAMPLE003"])                 # 기한 2026-05-31: 기준일 생략 시 오늘 기준
    assert not is_expired(by_id["SAMPLE001"])             # 2099-12-31


def test_apply_filters_keeps_order(sample_chunks):
    kept = apply_filters(sample_chunks, {"today": TODAY})
    assert kept == [c for c in sample_chunks if c in kept]
