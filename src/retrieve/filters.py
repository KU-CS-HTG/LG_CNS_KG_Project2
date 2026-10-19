"""정형 조건 필터 (기획서 8.1 메타데이터). 하드가 아니라 소프트로 적용한다.

- 값이 비어 있는 메타데이터는 제외하지 않고 후보에 남긴다 ('확인 필요'로 표시되도록 C에 전달).
- 신청기한이 지난 제도는 항상 제외한다.
- 결과가 없으면 relax()로 필터를 단계적으로 풀어 다시 검색한다.

벡터 저장소의 `vector_store.build_where`(저장소 안에서 거르는 쪽)와 규칙이 같다. 여기 `apply_filters`는
이미 꺼낸 Chunk 목록을 파이썬에서 거를 때(Baseline 결과, 후처리, 테스트) 쓴다. 두 규칙은 항상 같아야 한다
(tests/test_retrieval.py가 두 결과가 같은지 확인한다).

지역은 문자열이 정확히 같아야 한다("서울" != "서울특별시"). 지역명 정규화는 A가 메타데이터를 만들 때 맞춘다.
"""
from __future__ import annotations

import re
from datetime import date

from src.common.schema import Chunk, Filters

NATIONWIDE = "전국"
# 완화 순서: 덜 중요한 조건부터 푼다 (관심 분야 -> 지역 -> 연령). 신청기한(today)은 풀지 않는다.
RELAX_ORDER = ("field", "region", "age")


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
    region = filters.get("region")
    if region and md.get("region") and md["region"] not in (region, NATIONWIDE):
        return False
    field = filters.get("field")
    if field and md.get("field") and md["field"] != field:
        return False
    return True


def apply_filters(chunks: list[Chunk], filters: Filters) -> list[Chunk]:
    """filters에 맞는 조각만 남긴다 (순서 유지). 값이 비어 있는 항목은 통과시킨다."""
    return [c for c in chunks if _passes(c, filters)]


def relax(filters: Filters) -> Filters:
    """한 단계 완화: 관심 분야 -> 지역 -> 연령 순으로 하나씩 제거한다.

    세 조건이 모두 없으면 situation(검색에는 쓰이지 않음)을 제거하고, 더 풀 것이 없으면 같은 필터를 돌려준다.
    신청기한(today)은 항상 유지한다. 호출하는 쪽은 결과가 입력과 같으면 더 풀 수 없다고 보고 멈춘다.
    """
    relaxed = dict(filters)
    for key in RELAX_ORDER:
        if key in relaxed:
            del relaxed[key]
            return relaxed  # type: ignore[return-value]
    relaxed.pop("situation", None)
    return relaxed  # type: ignore[return-value]
