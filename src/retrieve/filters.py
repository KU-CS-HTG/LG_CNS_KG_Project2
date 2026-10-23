"""정형 조건 필터 (기획서 8.1 메타데이터). 하드가 아니라 소프트로 적용한다.

- 값이 비어 있는 메타데이터는 제외하지 않고 후보에 남긴다 ('확인 필요'로 표시되도록 C에 전달).
- 신청기한이 지난 제도는 항상 제외한다.
- 결과가 없으면 relax()로 필터를 단계적으로 풀어 다시 검색한다.
- 관심 분야(field)와 상황(situation)은 **필터가 아니다**. 자격이 아니라 관심사라서 거르지 않고(분야 이름이 데이터와 달라도
  제도가 사라지지 않게), 분야는 hybrid.search에서 맞는 제도에 가산점으로만 쓴다 (`field_matches`).

벡터 저장소의 `vector_store.build_where`(저장소 안에서 거르는 쪽)와 규칙이 같다. 여기 `apply_filters`는
이미 꺼낸 Chunk 목록을 파이썬에서 거를 때(Baseline 결과, 후처리, 테스트) 쓴다. 두 규칙은 항상 같아야 한다
(tests/test_retrieval.py가 두 결과가 같은지 확인한다).

지역 별칭은 shared canonical vocabulary로 비교한다. 기관 기반 추정도 기존 검색 필터에 영향을 주지만 자격 검증은 아니다.
"""
from __future__ import annotations

import re
from datetime import date

from src.common.schema import Chunk, Filters
from src.common.regions import normalize_region

NATIONWIDE = "전국"
# 완화 순서: 지역 -> 연령. 신청기한(today)은 풀지 않는다. field·situation은 필터가 아니므로 풀 대상이 아니다.
RELAX_ORDER = ("region", "age")


def _date_key(value: str | None) -> int | None:
    """'2026-12-31' / '20261231' -> 20261231. 날짜가 아니면 None (상시·미정)."""
    digits = re.sub(r"\D", "", value or "")
    return int(digits[:8]) if len(digits) >= 8 else None


def is_expired(chunk: Chunk, today: str | None = None) -> bool:
    """신청기한이 today(기본: 오늘)보다 이전이면 True. 기한이 없으면(상시·미정) False."""
    deadline = _date_key(chunk.metadata.get("deadline"))
    if deadline is None:
        return False
    return deadline < _date_key(today or date.today().isoformat())


def _passes(chunk: Chunk, filters: Filters) -> bool:
    md = chunk.metadata
    if is_expired(chunk, filters.get("today")):
        return False
    age = filters.get("age")
    if age is not None:
        if md.get("age_min") is not None and age < md["age_min"]:
            return False
        if md.get("age_max") is not None and age > md["age_max"]:
            return False
    region = normalize_region(filters.get("region"))
    stored_region = normalize_region(md.get("region")) or agency_region(md.get("agency"))
    if region and stored_region and stored_region not in (region, NATIONWIDE):
        return False
    return True                       # field·situation은 거르지 않는다 (가산점·참고용)


def apply_filters(chunks: list[Chunk], filters: Filters) -> list[Chunk]:
    """filters에 맞는 조각만 남긴다 (순서 유지). 값이 비어 있는 항목은 통과시킨다."""
    return [c for c in chunks if _passes(c, filters)]


_SIDO_FULL = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천", "광주광역시": "광주",
    "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종", "경기도": "경기", "강원도": "강원",
    "강원특별자치도": "강원", "충청북도": "충북", "충청남도": "충남", "전라북도": "전북", "전북특별자치도": "전북",
    "전라남도": "전남", "경상북도": "경북", "경상남도": "경남", "제주특별자치도": "제주", "제주도": "제주",
}
_SIDO_SHORT = sorted(set(_SIDO_FULL.values()), key=len, reverse=True)
_SIDO_BY_LENGTH = sorted(_SIDO_FULL, key=len, reverse=True)
_LOCAL_UNIT = re.compile(r"^[가-힣]{1,6}(시|군|구)(\s|$)")


def agency_region(agency: str | None) -> str | None:
    """소관기관명에서 시·도(짧은 이름: 서울·경기 …)를 추정한다. 시·도 이름이 없거나 전국 기관이면 None.

    시·도 이름으로 시작하면 그 지역이다 ('서울특별시 강서구' -> 서울, '경기주택도시공사' -> 경기). 지역을 알 수 없는
    시·군·구('안산시 단원구')나 중앙부처·공공기관은 None이다."""
    agency = (agency or "").strip()
    for full in _SIDO_BY_LENGTH:
        if agency.startswith(full):
            return _SIDO_FULL[full]
    for short in _SIDO_SHORT:
        if agency.startswith(short) and len(agency) > len(short):
            return short
    return None


def is_local(chunk: Chunk) -> bool:
    """지자체(지역) 제도인가. 저장된 metadata.region이 있으면 그것을, 없으면 소관기관명으로 추정한다.

    A의 지역 어댑터가 아직 region을 채우지 않아서(INGEST_A.md) 기관명으로 추정한다: 시·도 이름으로 시작하거나
    '○○시/군/구'로 시작하면 지역 제도, 그 밖(중앙부처·공공기관)은 전국 단위로 본다. 추정이므로 정확하지 않을 수 있다.
    """
    region = chunk.metadata.get("region")
    if region:
        return region != NATIONWIDE
    agency = (chunk.metadata.get("agency") or "").strip()
    return agency_region(agency) is not None or bool(_LOCAL_UNIT.match(agency))


def field_matches(chunk_field: str | None, wanted: str | None) -> bool:
    """제도의 분야가 사용자의 관심 분야와 맞는가 (가산점 판정용). 한쪽이 다른 쪽을 포함하면 맞는 것으로 본다.
    예: wanted "주거" / 제도 분야 "주거·자립" -> True. 어느 한쪽이 비어 있으면 False (가산점 없음, 거르지는 않음)."""
    if not chunk_field or not wanted:
        return False
    return wanted in chunk_field or chunk_field in wanted


def relax(filters: Filters) -> Filters:
    """한 단계 완화: 지역 -> 연령 순으로 하나씩 제거한다. 신청기한(today)은 항상 유지한다.

    field·situation은 필터가 아니므로 건드리지 않는다 (그대로 남겨, 호출하는 쪽이 '어떤 조건을 풀었는지'를
    입력과 비교하면 지역·연령만 나온다). 더 풀 것이 없으면 같은 필터를 돌려주므로, 결과가 입력과 같으면 멈춘다.
    """
    relaxed = dict(filters)
    for key in RELAX_ORDER:
        if key in relaxed:
            del relaxed[key]
            return relaxed  # type: ignore[return-value]
    return relaxed  # type: ignore[return-value]
