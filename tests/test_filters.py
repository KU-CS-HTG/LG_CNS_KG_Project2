"""B: retrieve/filters.py. 소프트 필터 규칙(값이 비면 통과, 기한 지난 제도 제외, 결과 없으면 완화)."""
from src.retrieve.filters import apply_filters, field_matches, is_expired, relax

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


def test_relax_eventually_stops():
    """지역·연령을 풀고 나면 더 풀 것이 없어 같은 필터를 돌려준다. field·situation·today는 그대로 남는다."""
    f = {"region": "서울", "age": 27, "field": "주거", "situation": ["구직"], "today": TODAY}
    for _ in range(10):
        nxt = relax(f)
        if nxt == f:
            break
        f = nxt
    else:
        raise AssertionError("relax가 멈추지 않음")
    assert f == {"field": "주거", "situation": ["구직"], "today": TODAY}


def test_relax_order_and_keeps_today():
    f = {"age": 27, "region": "서울", "field": "주거", "situation": ["구직"], "today": TODAY}
    steps = [f]
    while True:
        nxt = relax(steps[-1])
        if nxt == steps[-1]:
            break
        steps.append(nxt)
    removed = [set(a) - set(b) for a, b in zip(steps, steps[1:])]
    assert removed == [{"region"}, {"age"}]                                # 지역 -> 연령. 분야는 필터가 아니라 풀지 않는다
    assert all(s["today"] == TODAY and s["field"] == "주거" and s["situation"] == ["구직"] for s in steps)
    assert steps[-1] == {"field": "주거", "situation": ["구직"], "today": TODAY}


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


def test_field_is_not_a_filter(sample_chunks):
    base = _ids(sample_chunks, {"today": TODAY})
    assert _ids(sample_chunks, {"field": "주거", "today": TODAY}) == base
    assert _ids(sample_chunks, {"field": "없는분야", "today": TODAY}) == base


def test_field_matches():
    assert field_matches("주거", "주거") and field_matches("주거·자립", "주거")      # 한쪽이 다른 쪽을 포함하면 맞음
    assert field_matches("주거", "주거·자립")
    assert not field_matches("일자리", "주거")
    assert not field_matches("", "주거") and not field_matches("주거", None) and not field_matches(None, None)
